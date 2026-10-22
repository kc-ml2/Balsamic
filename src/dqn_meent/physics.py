"""TM-polarized 1D RCWA forward model, using the lab's MEENT (the meent-private fork) with Torch on CPU.

Power efficiencies are normalized to incident power. The design is viewed from
the incident glass half-space: glass -> patterned Si/air layer -> air. Positive
diffraction order follows MEENT's kx = kx_inc + m*lambda/period convention.
"""
from collections import OrderedDict
from dataclasses import asdict, dataclass

import numpy as np

from .config import PhysicsConfig
from .rcwa import load


@dataclass(frozen=True)
class ForwardResult:
    efficiency: float
    reflectance: float
    transmittance: float
    absorption: float
    orders: tuple[int, ...]
    transmission_orders: tuple[float, ...]
    reflection_orders: tuple[float, ...]

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        values = dict(data)
        for key in ("orders", "transmission_orders", "reflection_orders"):
            values[key] = tuple(values[key])
        return cls(**values)


def validate_design(design, n_cells):
    """Validate before conversion so floats, NaNs and wrong shapes cannot alias."""
    array = np.asarray(design)
    if array.shape != (n_cells,):
        raise ValueError(f"Design must have shape ({n_cells},), got {array.shape}")
    if not np.all((array == 0) | (array == 1)):
        raise ValueError("Design cells must be binary 0 (air) or 1 (silicon)")
    return array.astype(np.uint8, copy=True)


def silicon_index(config):
    """Return n-ik, MEENT's passive-material convention.

    Current MEENT tables use meters, although all RCWA geometry here uses nm.
    Earlier MEENT releases bundled the same table in nm. Explicit range checking
    avoids np.interp silently clamping unsupported wavelengths to the table edge.
    """
    if config.material == "constant":
        return complex(config.silicon_n, -config.silicon_k)
    load()
    from meent.on_torch.modeler.modeling import read_material_table

    table = np.asarray(read_material_table()["SI_GREEN-2008"])
    if table.ndim != 2 or table.shape[1] != 3:
        raise RuntimeError("Expected a tabulated wavelength/n/k Green silicon table")
    # Green's tabulated range is 250--1450 nm. Accept its two known encodings.
    if 1e-6 < table[-1, 0] < 2e-6:
        wavelength = config.wavelength_nm * 1e-9
    elif 1000 < table[-1, 0] < 2000:
        wavelength = config.wavelength_nm
    else:
        raise RuntimeError("Unrecognized wavelength units in MEENT Green Si table")
    if not table[0, 0] <= wavelength <= table[-1, 0]:
        raise ValueError("meent_green supports wavelengths only from 250 to 1450 nm")
    n = np.interp(wavelength, table[:, 0], table[:, 1])
    k = np.interp(wavelength, table[:, 0], table[:, 2])
    return complex(n, -k)


class ForwardSolver:
    """Deterministic RCWA with an in-memory, bounded exact-design LRU cache.

    `evaluations` counts valid evaluation requests; `solver_calls` counts actual
    RCWA invocations; `cache_hits` counts reused designs. Cache keys retain the
    exact origin of the design, with no reflection/translation canonicalization.
    """

    def __init__(self, config: PhysicsConfig):
        self.config = config
        self.silicon_index = silicon_index(config)
        self.evaluations = 0
        self.solver_calls = 0
        self.cache_hits = 0
        self._cache = OrderedDict()
        meent, self._torch = load()
        # A 1D cell has 2*fourier_order+1 harmonics (31 by default): a CUDA launch would cost more than the solve.
        self._solver = meent.call_mee(
            device="cpu",
            n_top=config.n_incident,
            n_bot=config.n_exit,
            theta=0,
            phi=None,
            pol=1,  # 1 = TM, electric field in the x-z plane.
            period=[config.period_nm],
            wavelength=config.wavelength_nm,
            thickness=[config.thickness_nm],
            fto=[config.fourier_order, 0],
            type_complex=self._torch.complex128,
            fourier_type=1,  # continuous integration of piecewise-constant cells
        )

    def state_dict(self):
        """Capture counters and LRU order for an exact checkpoint continuation."""
        return {
            "config": asdict(self.config),
            "evaluations": self.evaluations,
            "solver_calls": self.solver_calls,
            "cache_hits": self.cache_hits,
            "cache": [(key.hex(), value.to_dict()) for key, value in self._cache.items()],
        }

    def load_state_dict(self, state):
        if state["config"] != asdict(self.config):
            raise ValueError("Cannot restore solver state with different physics")
        self.evaluations = int(state["evaluations"])
        self.solver_calls = int(state["solver_calls"])
        self.cache_hits = int(state["cache_hits"])
        self._cache = OrderedDict(
            (bytes.fromhex(key), ForwardResult.from_dict(value))
            for key, value in state["cache"]
        )

    def evaluate(self, binary: np.ndarray) -> ForwardResult:
        design = validate_design(binary, self.config.n_cells)
        self.evaluations += 1
        key = np.packbits(design).tobytes()
        if key in self._cache:
            self.cache_hits += 1
            self._cache.move_to_end(key)
            return self._cache[key]

        indices = np.where(design == 1, self.silicon_index, self.config.n_exit)
        self._solver.ucell = self._torch.from_numpy(indices.astype(np.complex128).reshape(1, 1, -1))
        self.solver_calls += 1
        with self._torch.no_grad():
            result = self._solver.conv_solve()
        reflected = result.de_ri.numpy().astype(float).reshape(-1)
        transmitted = result.de_ti.numpy().astype(float).reshape(-1)
        count = 2 * self.config.fourier_order + 1
        if reflected.size != count or transmitted.size != count:
            raise RuntimeError("Unexpected MEENT diffraction-order array shape")
        powers = np.concatenate([reflected, transmitted])
        tolerance = self.config.energy_tolerance
        if not np.isfinite(powers).all() or np.min(powers) < -tolerance:
            raise FloatingPointError("RCWA produced invalid diffraction efficiencies")
        # Remove floating-point negative zeros, but reject material/model errors.
        reflected = np.maximum(reflected, 0.0)
        transmitted = np.maximum(transmitted, 0.0)
        reflectance, transmittance = float(reflected.sum()), float(transmitted.sum())
        total_power = reflectance + transmittance
        if total_power > 1.0 + tolerance:
            raise FloatingPointError(
                f"Passive RCWA model violates energy balance: R+T={total_power:.12g}"
            )
        if self.silicon_index.imag == 0 and abs(total_power - 1.0) > tolerance:
            raise FloatingPointError(
                f"Lossless RCWA model violates energy conservation: R+T={total_power:.12g}"
            )
        orders = tuple(range(-self.config.fourier_order, self.config.fourier_order + 1))
        output = ForwardResult(
            efficiency=float(transmitted[self.config.fourier_order + 1]),
            reflectance=reflectance,
            transmittance=transmittance,
            absorption=max(0.0, 1.0 - total_power),
            orders=orders,
            transmission_orders=tuple(float(x) for x in transmitted),
            reflection_orders=tuple(float(x) for x in reflected),
        )
        if self.config.cache_size:
            self._cache[key] = output
            if len(self._cache) > self.config.cache_size:
                self._cache.popitem(last=False)
        return output
