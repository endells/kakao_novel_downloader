from plugin import create_plugin_instance

P = create_plugin_instance({
    'filepath': __file__, 'use_db': True, 'use_default_setting': True,
    'home_module': 'basic', 'setting_menu': None, 'default_route': 'normal',
    'menu': {'uri': __package__, 'name': '카카오소설 다운로더', 'list': [
        {'uri': 'basic/works', 'name': '내 작품 / 진행'},
        {'uri': 'basic/add', 'name': '작품 등록 / 수동'},
        {'uri': 'basic/copy', 'name': '시즌별 파일 복사'},
        {'uri': 'basic/setting', 'name': '설정 / 스케줄'},
        {'uri': 'basic/logs', 'name': '실행 로그'},
        {'uri': 'basic/history', 'name': '이력 / 오류'},
    ]},
})

from .mod_basic import ModuleBasic
P.set_module_list([ModuleBasic])
