"""Field checks against the native API, analytic illumination, and power flux."""
import numpy as np
import pytest

pytest.importorskip("torch")
from meent.on_torch.emsolver._base import THETA_DETUNE   # the fork runs normal incidence at this angle, in radians

from dqn_meent.config import PhysicsConfig
from dqn_meent.fields import padded_solver, reconstruct_tm, sample_fields
from dqn_meent.physics import ForwardSolver


def test_reconstruction_matches_native_meent_and_padding_preserves_diffraction():
    config = PhysicsConfig(n_cells=16, fourier_order=5)
    design = np.random.default_rng(71).integers(2, size=16)
    solver, result = padded_solver(config, design, glass_nm=50, air_nm=400)
    # The fork's public field is [z, y, x, (Ex, Ey, Ez, Z0Hx, Z0Hy, Z0Hz)] with z running up from the exit medium and
    # unit incident E; reconstruct_tm runs from the incident medium down with unit incident Hy.
    public = solver.calculate_field(res_x=31, res_y=1, res_z=9).numpy()[::-1, 0] / config.n_incident
    native = public[..., [4, 0, 2]]
    efficient = np.concatenate(reconstruct_tm(
        solver, np.linspace(0, config.period_nm, 31),
        [np.linspace(0, d, 9) for d in (50, config.thickness_nm, 400)],
    ))
    np.testing.assert_allclose(efficient, native, atol=3e-12)
    original = ForwardSolver(config).evaluate(design)
    np.testing.assert_allclose(np.asarray(result.de_ti).ravel(), original.transmission_orders, atol=2e-11)
    np.testing.assert_allclose(np.asarray(result.de_ri).ravel(), original.reflection_orders, atol=2e-11)


def test_incident_normalization_in_homogeneous_medium():
    config = PhysicsConfig(n_cells=16, fourier_order=3, n_incident=1.45, n_exit=1.45)
    fields = sample_fields(config, np.zeros(16), nx=32, nz_pattern=12)
    np.testing.assert_allclose(np.abs(fields["ex"]), 1, atol=1e-12)
    # A plane wave at the solver's angle: |Ez| = sin(theta), zero at exact normal incidence.
    np.testing.assert_allclose(np.abs(fields["ez"]), np.sin(THETA_DETUNE), atol=1e-12)
    expected = np.exp(-2j * np.pi * config.n_incident * fields["height_nm"] / config.wavelength_nm)
    np.testing.assert_allclose(fields["ex"][:, 0], expected, atol=1e-12)
    assert fields["exit_flux"] == pytest.approx(1, abs=1e-12)


def test_field_flux_matches_power_and_fresnel_electric_amplitude():
    config = PhysicsConfig(n_cells=16, fourier_order=7)
    empty = sample_fields(config, np.zeros(16), nx=64, nz_pattern=16)
    t_electric = 2 * config.n_incident / (config.n_incident + config.n_exit)
    np.testing.assert_allclose(np.abs(empty["ex"][-1]), t_electric, atol=1e-12)
    assert empty["exit_flux"] == pytest.approx(empty["transmittance"], abs=1e-12)
    pattern = np.random.default_rng(19).integers(2, size=16)
    fields = sample_fields(config, pattern, nx=64, nz_pattern=16)
    assert fields["exit_flux"] == pytest.approx(fields["transmittance"], abs=2e-11)
    assert fields["reflectance"] + fields["exit_flux"] == pytest.approx(1, abs=2e-11)


def test_outgoing_electric_field_is_transverse_in_plotted_height_coordinates():
    config = PhysicsConfig(n_cells=16, fourier_order=5)
    design = np.random.default_rng(71).integers(2, size=16)
    fields = sample_fields(config, design, nx=64, nz_pattern=16)
    for order in (-1,0,1):
        basis = np.exp(2j*np.pi*order*fields["x_nm"]/config.period_nm)
        ex = np.mean(fields["ex"][-1]*basis)
        ez = np.mean(fields["ez"][-1]*basis)
        kx = order*config.wavelength_nm/config.period_nm + config.n_incident*np.sin(THETA_DETUNE)
        kz = np.sqrt(config.n_exit**2-kx**2+0j)
        # At the fork's normal-incidence angle the +m and -m modes are split by about THETA_DETUNE and mixed, so the field is
        # transverse to that order (1e-12 with the release's 1e-20); a wrong rotation sign would leave a residual of order 1.
        assert abs(kx*ex+kz*ez) < 10 * THETA_DETUNE
