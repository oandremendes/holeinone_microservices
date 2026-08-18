import api_config


def test_defaults_from_empty_config():
    assert api_config.legacy_apis_enabled(config={}) is False
    a = api_config.get_anthropic(config={'anthropic': {'api_key': 'k'}})
    assert a == {'api_key': 'k', 'model': 'claude-opus-5'}
    o = api_config.get_odoo(config={'odoo': {'webhook_url': 'u', 'api_key': 'k'}})
    assert o['drive_remote'] == 'gdrive:ScanSnap'
    assert o['drive_lsf_flags'] == []
    o2 = api_config.get_odoo(config={'odoo': {
        'drive_lsf_flags': ['--drive-shared-with-me']}})
    assert o2['drive_lsf_flags'] == ['--drive-shared-with-me']


def test_legacy_flag_reads_config():
    assert api_config.legacy_apis_enabled(
        config={'legacy_apis': {'enabled': True}}) is True
