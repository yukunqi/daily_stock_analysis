import argparse

from main import _is_explicit_one_shot_run


def _args(**overrides):
    defaults = {
        "schedule": False,
        "serve": False,
        "serve_only": False,
        "webui": False,
        "webui_only": False,
        "market_review": False,
        "backtest": False,
        "stocks": None,
        "dry_run": False,
        "no_notify": False,
        "single_notify": False,
        "workers": None,
        "no_market_review": False,
        "force_run": False,
        "no_context_snapshot": False,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_stocks_dry_run_is_explicit_one_shot():
    assert _is_explicit_one_shot_run(_args(stocks="600519,AAPL", dry_run=True))


def test_no_market_review_is_explicit_one_shot():
    assert _is_explicit_one_shot_run(_args(no_market_review=True))


def test_schedule_mode_is_not_one_shot():
    assert not _is_explicit_one_shot_run(_args(schedule=True, stocks="600519"))


def test_serve_mode_is_not_one_shot():
    assert not _is_explicit_one_shot_run(_args(serve=True, dry_run=True))


def test_default_args_are_not_explicit_one_shot():
    assert not _is_explicit_one_shot_run(_args())
