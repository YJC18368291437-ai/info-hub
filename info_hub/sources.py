def accepts_source(settings, self_id, group_id):
    return bool(settings.groups and group_id in settings.groups and self_id == settings.self_qq)


def is_course_source(settings, group_id):
    if settings.groups.get(group_id,{}).get('course'):
        return True
    if group_id in ('123456781','123456784','123456783','123456782'):
        return True
    name = settings.group_name(group_id)
    return '某学院' in name or '某班' in name or '某班' in name
