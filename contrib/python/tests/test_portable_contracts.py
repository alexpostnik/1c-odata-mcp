"""Public policy must be explicit and portable; fixtures are synthetic."""
from datetime import date
import pytest
from onec_odata_mcp.cash_revenue import rko_cleanup_candidates
from onec_odata_mcp.metadata import MetadataModel
from onec_odata_mcp.person_repair import seal_plan


def test_cleanup_uses_explicit_period_instead_of_private_cutoff():
    proofs = dict.fromkeys(['full_starting_set','full_source_coverage','same_test_ledger','claim_continuity','held_identity_resolved'], True)
    rows = [{'target_id':'synthetic-target', 'date':'2024-01-01','unpost_verified':True,'posted':False,'deletion_mark':False,'fresh_version':'synthetic-version','held':False}]
    result = rko_cleanup_candidates(rows, [], [], proofs, period_start=date(2024,1,1), period_end=date(2024,12,31))
    assert isinstance(result,dict)


def test_enum_dispatch_mapping_is_opt_in_and_empty_by_default():
    model = MetadataModel()
    with pytest.raises(ValueError,match='mapping'):
        model.resolve_enum_value('SyntheticEnum','SyntheticMember',field='SyntheticField',account_context='synthetic_context')
    assert model.enum_context_mappings == {}


def test_repair_plan_uses_generic_profile_and_arbitrary_nonempty_batch():
    plan = seal_plan({'index_sha256':'a'*64}, [{'synthetic':True}])
    assert plan['profile'] == 'person-repair-v1'
    assert 'issue' not in plan
