"""On-device hold-to-talk for an explicitly selected Codex thread."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from heard import notify, ptt_indicator


@dataclass(frozen=True)
class ReplyTarget:
    thread_id: str
    name: str


def selected_target(status: dict) -> ReplyTarget | None:
    """Use the explicit pin, never whichever agent happened to speak last."""
    for session in status.get("active_sessions", ()):
        if session.get("agent") != "codex" or not session.get("pinned"):
            continue
        try:
            thread_id = str(UUID(session["session_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        return ReplyTarget(thread_id, session.get("repo_name") or "Codex")
    return None


def queue_reply(target: ReplyTarget, text: str) -> None:
    """Queue a prompt by thread UUID using the native Codex installation."""
    candidates = (
        Path.home() / ".local/bin/codex",
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
    )
    codex = next((str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)
    codex = codex or shutil.which("codex")
    if codex is None:
        raise FileNotFoundError("Install Codex CLI to send voice replies.")
    result = subprocess.run(
        [codex, "queue", "--thread", target.thread_id, f"--message={text}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=False,
    )
    if result.returncode:
        raise RuntimeError("Codex could not queue the reply. Check that the selected thread is accessible locally.")


class LocalVoice:
    """Own a single microphone capture on the menu bar app's run loop."""

    def __init__(self) -> None:
        from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
        from Speech import SFSpeechRecognizer

        self._device = AVCaptureDevice
        self._audio = AVMediaTypeAudio
        self._speech = SFSpeechRecognizer
        self._recognizer = SFSpeechRecognizer.alloc().init()
        self._capture: _Capture | None = None
        self._requesting_permission = False

    def request_permissions(self) -> bool:
        """Request missing macOS permissions without recording."""
        from AVFoundation import AVAuthorizationStatusAuthorized, AVAuthorizationStatusNotDetermined
        from Foundation import NSBundle
        from PyObjCTools.AppHelper import callAfter
        from Speech import SFSpeechRecognizerAuthorizationStatusAuthorized as speech_authorized
        from Speech import SFSpeechRecognizerAuthorizationStatusNotDetermined as speech_undetermined

        if not NSBundle.mainBundle().objectForInfoDictionaryKey_("NSSpeechRecognitionUsageDescription"):
            notify.notify("Heard voice reply", "Open the built Heard.app to grant speech recognition permission.")
            return False
        if self._requesting_permission:
            return False
        mic = self._device.authorizationStatusForMediaType_(self._audio)
        speech = self._speech.authorizationStatus()
        if mic == AVAuthorizationStatusAuthorized and speech == speech_authorized:
            return True
        self._requesting_permission = True

        def resume(_result) -> None:
            def check() -> None:
                self._requesting_permission = False
                self.request_permissions()

            callAfter(check)

        if mic == AVAuthorizationStatusNotDetermined:
            self._device.requestAccessForMediaType_completionHandler_(self._audio, resume)
        elif mic == AVAuthorizationStatusAuthorized and speech == speech_undetermined:
            self._speech.requestAuthorization_(resume)
        else:
            self._requesting_permission = False
            notify.notify(
                "Heard voice reply", "Allow Heard in macOS Microphone and Speech Recognition settings.",
                kind="local_voice_permissions",
            )
        return False

    def start(self, target: ReplyTarget) -> None:
        """Begin recording for the target captured on key-down."""
        from AVFoundation import AVAudioEngine
        from objc import error as objc_error
        from PyObjCTools.AppHelper import callAfter, callLater
        from Speech import SFSpeechAudioBufferRecognitionRequest

        if self._capture is not None or not self.request_permissions():
            return
        recognizer = self._recognizer
        if recognizer is None or not recognizer.supportsOnDeviceRecognition() or not recognizer.isAvailable():
            notify.notify("Heard voice reply", "On-device recognition is unavailable for your Mac’s speech language.")
            return
        request = SFSpeechAudioBufferRecognitionRequest.alloc().init()
        request.setRequiresOnDeviceRecognition_(True)
        request.setShouldReportPartialResults_(False)
        engine = AVAudioEngine.alloc().init()
        node = engine.inputNode()
        capture = _Capture(target, request, engine, node)
        self._capture = capture

        def receive(result, error) -> None:
            if result is not None and result.isFinal():
                callAfter(self._finish, capture, str(result.bestTranscription().formattedString()).strip())
            elif error is not None:
                callAfter(self._finish, capture, "")

        try:
            audio_format = node.outputFormatForBus_(0)
            if not audio_format.sampleRate() or not audio_format.channelCount():
                raise RuntimeError("The selected microphone has no input channels.")
            node.installTapOnBus_bufferSize_format_block_(
                0, 1024, audio_format, lambda buffer, _: request.appendAudioPCMBuffer_(buffer),
            )
            capture.tapping = True
            started, _ = engine.startAndReturnError_(None)
            if not started:
                raise RuntimeError("Could not open the microphone.")
            capture.task = recognizer.recognitionTaskWithRequest_resultHandler_(request, receive)
        except (RuntimeError, ValueError, objc_error):
            self._clear(capture)
            notify.notify("Heard voice reply", "Could not start recording. Check macOS Sound → Input.")
            return
        ptt_indicator.show()
        callLater(55.0, self._expire, capture)

    def release(self) -> None:
        """Close the microphone and let Speech finalize the transcript."""
        from PyObjCTools.AppHelper import callLater

        capture = self._capture
        if capture is None or capture.released:
            return
        capture.released = True
        capture.stop_audio()
        ptt_indicator.hide()
        capture.request.endAudio()
        callLater(5.0, self._expire, capture)

    def cancel(self) -> None:
        """Discard an outstanding recording when local voice is disabled."""
        capture = self._capture
        if capture is not None:
            self._clear(capture)

    def _expire(self, capture: _Capture) -> None:
        self._finish(capture, "")

    def _clear(self, capture: _Capture) -> None:
        self._capture = None
        capture.stop_audio()
        ptt_indicator.hide()
        if capture.task is not None:
            capture.task.cancel()

    def _finish(self, capture: _Capture, text: str) -> None:
        if self._capture is not capture:
            return
        self._clear(capture)
        if capture.released and text:
            threading.Thread(target=_send, args=(capture.target, text), daemon=True).start()
        else:
            notify.notify("Heard voice reply", "No complete recording was sent. Hold right Command and try again.")


@dataclass
class _Capture:
    target: ReplyTarget
    request: object
    engine: object
    node: object
    tapping: bool = False
    released: bool = False
    task: object | None = None

    def stop_audio(self) -> None:
        if self.tapping:
            self.tapping = False
            self.engine.stop()
            self.node.removeTapOnBus_(0)


def _send(target: ReplyTarget, text: str) -> None:
    try:
        queue_reply(target, text)
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        notify.notify("Heard voice reply", f"Could not confirm a reply to {target.name}. Check the Codex thread.")
    else:
        notify.notify("Heard voice reply", f"Queued a reply for {target.name}.")
