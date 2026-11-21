from types import SimpleNamespace
from uuid import uuid4

from heard import local_voice
from heard.multi_agent import MultiAgentRouter


def test_reply_target_is_explicit_codex_pin():
    router = MultiAgentRouter()
    first, second, claude = (str(uuid4()) for _ in range(3))
    router.note_event(first, agent="codex")
    router.note_event(second, agent="codex")
    router.note_event(claude, agent="claude")
    assert local_voice.selected_target({"active_sessions": router.list_active()}) is None
    router.pin(claude)
    assert local_voice.selected_target({"active_sessions": router.list_active()}) is None
    router.pin(first)
    target = local_voice.selected_target({"active_sessions": router.list_active()})
    assert target.thread_id == first


def test_transcript_queues_to_exact_thread_as_one_argument(monkeypatch):
    calls = []
    monkeypatch.setattr(
        local_voice.subprocess, "run",
        lambda args, **_kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(local_voice.shutil, "which", lambda _name: "/usr/local/bin/codex")
    target = local_voice.ReplyTarget(str(uuid4()), "Build")
    local_voice.queue_reply(target, "--not a flag\nPlease check `main`.")
    assert calls[0][1:] == ["queue", "--thread", target.thread_id, "--message=--not a flag\nPlease check `main`."]


def test_only_final_after_key_release_is_sent(monkeypatch):
    calls = []
    monkeypatch.setattr(local_voice.ptt_indicator, "hide", lambda: None)
    monkeypatch.setattr(local_voice.notify, "notify", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(local_voice, "_send", lambda *args: calls.append(args))
    monkeypatch.setattr(
        local_voice.threading, "Thread",
        lambda target, args, **_kwargs: SimpleNamespace(start=lambda: target(*args)),
    )
    controller = local_voice.LocalVoice.__new__(local_voice.LocalVoice)
    target = local_voice.ReplyTarget(str(uuid4()), "Build")
    capture = local_voice._Capture(target, None, None, None)
    controller._capture = capture
    controller._finish(capture, "early callback")
    controller._finish(capture, "late callback")
    assert calls == []

    released = local_voice._Capture(target, None, None, None, released=True)
    controller._capture = released
    controller._finish(released, "final transcript")
    assert calls == [(target, "final transcript")]
