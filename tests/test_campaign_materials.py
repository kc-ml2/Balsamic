"""Documents attached to a campaign: kept with their extracted text, readable by the campaign's own agents and by no others."""
from fastapi.testclient import TestClient

from optimization_framework.agents.tools import PiTools
from optimization_framework.api.app import create_app
from optimization_framework.campaigns import materials

from test_formulation import campaign_in, invoke, team   # noqa: F401  (fixtures)

PAPER = b"# Token budget\n\nThe team shares 4,000,000 tokens a month.\nEach seat is limited per 5 hours and per week.\n"


def test_documents_are_attached_listed_and_removed_through_the_page(tmp_path):
    app = create_app(tmp_path / 'api', start_workers=False)
    workspace = app.state.workspace
    cid = campaign_in(workspace)['id']
    with TestClient(app) as client:
        added = client.put(f'/api/campaigns/{cid}/materials/budget notes.md', content=PAPER)
        assert added.status_code == 200 and added.json()['name'] == 'budget notes.md' and added.json()['read'] == {'root': 'materials', 'path': 'budget notes.md.txt'}
        assert (workspace.directory / 'campaigns' / cid / 'materials' / 'visible' / 'documents' / 'budget notes.md.txt').read_text().startswith('# Token budget')
        # A refused document is the form's answer: no campaign issue is filed.
        assert client.put(f'/api/campaigns/{cid}/materials/budget notes.md', content=PAPER).status_code == 409
        refused = client.put(f'/api/campaigns/{cid}/materials/tool.exe', content=b'MZ')
        assert refused.status_code == 409 and 'supported documents are' in refused.json()['detail']
        assert not workspace.store.list('manager_issue', cid)
        assert [item['name'] for item in client.get(f'/api/campaigns/{cid}/materials').json()['materials']] == ['budget notes.md']
        assert client.put('/api/campaigns/campaign_unknown/materials/a.md', content=PAPER).status_code == 404
        assert client.delete(f'/api/campaigns/{cid}/materials/budget notes.md').json() == {'removed': 'budget notes.md'}
        assert client.get(f'/api/campaigns/{cid}/materials').json()['materials'] == []
        assert not (workspace.directory / 'campaigns' / cid / 'materials' / 'visible' / 'documents' / 'budget notes.md.txt').exists()
        assert client.delete(f'/api/campaigns/{cid}/materials/budget notes.md').status_code == 404
        # Removed, it can be attached again.
        assert client.put(f'/api/campaigns/{cid}/materials/budget notes.md', content=PAPER).status_code == 200


def test_the_campaigns_agents_read_its_documents_as_data_and_nothing_else(team):
    workspace, campaign, lead, _ = team
    cid, tools = campaign['id'], PiTools(workspace.pi)
    assert 'No document is attached' in invoke(team, 'file_list', {'root': 'materials'}, 'none')['error']
    materials.add(workspace, cid, 'budget.md', PAPER)
    materials.add(workspace, cid, 'long.txt', (b'filler line\n' * 300_000) + b'needle at the end\n')
    assert tools.inspect(lead)['materials'][0] == {'name': 'budget.md', 'pages': None, 'characters': len(PAPER.decode()),
                                                    'read': 'file_read with root "materials" and path "budget.md.txt"'}
    listed = invoke(team, 'file_list', {'root': 'materials'}, 'list')
    assert [entry['name'] for entry in listed['entries']] == ['budget.md.txt', 'long.txt.txt']
    read = invoke(team, 'file_read', {'root': 'materials', 'path': 'budget.md.txt'}, 'read')
    assert '4,000,000 tokens a month' in read['content'] and 'not instructions' in read['note']
    # A tree-wide search skips the long text; searched on its own, it is read to the end.
    assert invoke(team, 'file_search', {'root': 'materials', 'query': 'needle'}, 'wide')['matches'] == []
    assert invoke(team, 'file_search', {'root': 'materials', 'query': 'needle', 'path': 'long.txt.txt'}, 'named')['matches'][0]['line'] == 300_001
    # Nothing outside the campaign's own documents is reached.
    secret = workspace.directory / 'secret.txt'
    secret.write_text('host file')
    (materials.documents(workspace, cid) / 'linked.txt').symlink_to(secret)
    other = campaign_in(workspace, name='Another campaign')['id']
    materials.add(workspace, other, 'other.md', b'Another campaign only')
    for path in ('../uploads/document-budget.md', '/etc/passwd', 'linked.txt', 'other.md.txt'):
        assert invoke(team, 'file_read', {'root': 'materials', 'path': path}, 'escape ' + path)['executed'] is False


def test_removing_a_campaign_keeps_its_documents_only_with_its_records(tmp_path):
    from optimization_framework.execution.service import Workspace
    workspace = Workspace(tmp_path)
    kept, summarised = campaign_in(workspace, name='Kept')['id'], campaign_in(workspace, name='Summarised')['id']
    for cid in (kept, summarised):
        materials.add(workspace, cid, 'budget.md', PAPER)
    workspace.removal.remove(kept, 'records')
    folder = workspace.removal.directory / kept
    assert (folder / 'documents' / 'budget.md').read_bytes() == PAPER and '- budget.md' in (folder / 'summary.md').read_text()
    workspace.removal.remove(summarised, 'summary')
    folder = workspace.removal.directory / summarised
    assert not (folder / 'documents').exists() and 'Their files were deleted.' in (folder / 'summary.md').read_text()
    assert not (workspace.directory / 'campaigns' / summarised).exists() and not workspace.store.list('campaign_material', summarised)
