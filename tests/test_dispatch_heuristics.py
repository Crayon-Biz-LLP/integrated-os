from core.webhook.dispatch import _format_task_line, resolve_dates_from_query

def test_format_task_line():
    assert _format_task_line("Clean desk", "HOME") == "Clean desk [HOME]"
    assert _format_task_line("Buy groceries", "") == "Buy groceries []"
    assert _format_task_line("Call Bob HOME", "HOME") == "Call Bob [HOME]"
    assert _format_task_line("Call Bob HOME", "home") == "Call Bob [home]"
    assert _format_task_line("Important task", "WORK", priority="high") == "Important task [WORK] (high)"

def test_resolve_dates_from_query():
    # Since resolve_dates uses datetime.now(), we just check if it returns valid tuples
    # and handles keywords correctly.
    
    start, end = resolve_dates_from_query("what's on my schedule this week?")
    assert start is not None
    assert end is not None
    assert end > start
    assert (end - start).days == 6
    
    start, end = resolve_dates_from_query("what about next week")
    assert start is not None
    assert end is not None
    assert (end - start).days == 6
    
    start, end = resolve_dates_from_query("show me tomorrow")
    assert start is not None
    assert end is not None
    assert (end - start).seconds == 86399 # 23 hours, 59 mins, 59 secs
    
    start, end = resolve_dates_from_query("what about today?")
    assert start is not None
    assert end is not None
    assert (end - start).seconds == 86399
    
    start, end = resolve_dates_from_query("no dates mentioned here")
    assert start is None
    assert end is None