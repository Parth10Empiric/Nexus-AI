"""
Unit tests for the tracker's pure logic: filtering, change-detection,
heartbeats, and DB writes. These don't need a real X11 display — we feed in
synthetic WindowSamples.

Run:  python -m pytest tests/ -v
   or just: python tests/test_tracker.py
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import config, filters          # noqa: E402
from tracker.db import ActivityStore          # noqa: E402
from tracker.window_source import WindowSample  # noqa: E402


def sample(app="code", title="tracker.py - nexus", pid=1234):
    return WindowSample(app_name=app, title=title, pid=pid)


class TestFilters(unittest.TestCase):
    def test_real_window_is_kept(self):
        self.assertTrue(filters.is_meaningful(sample()))

    def test_gnome_shell_is_noise(self):
        self.assertFalse(filters.is_meaningful(sample(app="gnome-shell", title="x")))

    def test_desktop_title_is_noise(self):
        self.assertFalse(filters.is_meaningful(sample(app="nautilus", title="Desktop")))

    def test_empty_title_is_noise(self):
        self.assertFalse(filters.is_meaningful(sample(title="")))


class TestStore(unittest.TestCase):
    def test_log_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            store = ActivityStore(Path(d) / "t.db")
            store.log(sample(title="A"))
            store.log(sample(title="B"))
            rows = store.recent(10)
            store.close()
            titles = {r["title"] for r in rows}
            self.assertEqual(titles, {"A", "B"})


class TestChangeDetection(unittest.TestCase):
    """Exercises Tracker._should_log without touching X11 or the real DB."""

    def _make_tracker(self):
        from unittest import mock
        with mock.patch("tracker.tracker.build_window_source"), \
             mock.patch("tracker.tracker.ActivityStore"):
            from tracker.tracker import Tracker
            return Tracker()

    def test_first_sample_logs_as_switch(self):
        t = self._make_tracker()
        self.assertEqual(t._should_log(sample(title="A"), now=100.0), "switch")

    def test_same_window_is_skipped(self):
        t = self._make_tracker()
        t._last_key = ("code", "A")
        t._last_log_time = 100.0
        self.assertIsNone(t._should_log(sample(title="A"), now=101.0))

    def test_changed_window_logs(self):
        t = self._make_tracker()
        t._last_key = ("code", "A")
        t._last_log_time = 100.0
        self.assertEqual(t._should_log(sample(title="B"), now=101.0), "switch")

    def test_heartbeat_after_interval(self):
        t = self._make_tracker()
        t._last_key = ("code", "A")
        t._last_log_time = 100.0
        later = 100.0 + config.HEARTBEAT_SECONDS + 1
        self.assertEqual(t._should_log(sample(title="A"), now=later), "heartbeat")


class TestTitleParsing(unittest.TestCase):
    KW = ["EMPIRA_HR", "Nexus AI"]

    def test_vscode_title(self):
        from tracker.file_resolver import parse_title
        p = parse_title("views.py - EMPIRA_HR - Visual Studio Code", self.KW)
        self.assertEqual(p.file_name, "views.py")
        self.assertEqual(p.project_keyword, "EMPIRA_HR")

    def test_dirty_marker_stripped(self):
        from tracker.file_resolver import parse_title
        p = parse_title("● tracker.py - Nexus AI - Visual Studio Code", self.KW)
        self.assertEqual(p.file_name, "tracker.py")

    def test_browser_tab_returns_none(self):
        from tracker.file_resolver import parse_title
        self.assertIsNone(parse_title("Some Article - YouTube - Chrome", self.KW))

    def test_file_without_known_project(self):
        from tracker.file_resolver import parse_title
        p = parse_title("main.go - Visual Studio Code", self.KW)
        self.assertEqual(p.file_name, "main.go")
        self.assertIsNone(p.project_keyword)


class TestDuplicateResolution(unittest.TestCase):
    def test_newest_duplicate_wins(self):
        import os
        from tracker.file_resolver import FileResolver
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "a").mkdir()
            (root / "b" / "node_modules").mkdir(parents=True)
            old = root / "a" / "views.py"
            new = root / "b" / "views.py"
            noise = root / "b" / "node_modules" / "views.py"
            for f in (old, new, noise):
                f.write_text("x = 1\n")
            # Make `new` the most recently modified.
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (2_000_000, 2_000_000))

            r = FileResolver.__new__(FileResolver)  # bypass config load
            r._cache_path = None
            r._cache_mtime = None
            chosen = r._search_workspace(root, "views.py")
            # node_modules copy must be pruned; newest of the rest chosen.
            self.assertEqual(chosen, new)


class TestFileContextStore(unittest.TestCase):
    def test_upsert_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            store = ActivityStore(Path(d) / "t.db")
            store.save_file_context(
                window_title="views.py - X", app_name="code",
                file_name="views.py", absolute_path="/x/views.py",
                file_content="v1",
            )
            store.save_file_context(
                window_title="views.py - X", app_name="code",
                file_name="views.py", absolute_path="/x/views.py",
                file_content="v2-updated",
            )
            row = store.latest_file_context()
            store.close()
            # Same path -> still one row, content overwritten (len of v2 = 10).
            self.assertEqual(row["content_len"], len("v2-updated"))


class TestContextMixer(unittest.TestCase):
    def _ext(self):
        from tracker.context_mixer import FileContext
        return FileContext(
            file_name="views.py",
            absolute_path="/home/empiric/Projects/EMPIRA_HR/backend/views.py",
            file_content="def f():\n    return 1\n",
            window_title="views.py - EMPIRA_HR - Code",
        )

    def test_external_file_is_injected(self):
        from tracker.context_mixer import build_system_prompt
        p = build_system_prompt(self._ext())
        self.assertIn("Current Open File: views.py", p)
        self.assertIn("```python", p)
        self.assertIn('Do not say "Sure, here is the fix".', p)

    def test_nexus_own_file_excluded(self):
        from unittest import mock
        from tracker import config
        from tracker.context_mixer import FileContext, build_system_prompt, is_self_referential
        c = FileContext("db.py", "/home/empiric/Projects/Nexus AI/tracker/db.py",
                        "x = 1", "db.py - Nexus AI - Code")
        with mock.patch.object(config, "EXCLUDE_SELF_CONTEXT", True):
            self.assertTrue(is_self_referential(c))
            self.assertNotIn("ACTIVE CODE CONTEXT", build_system_prompt(c))

    def test_marker_variants_excluded(self):
        from unittest import mock
        from tracker import config
        from tracker.context_mixer import FileContext, is_self_referential
        with mock.patch.object(config, "EXCLUDE_SELF_CONTEXT", True):
            for path in ("/opt/nexus_ai/m.py", "/x/NEXUS-AI/y.py", "/a/Nexus AI/z.py"):
                self.assertTrue(is_self_referential(FileContext("z.py", path, "c", "")))

    def test_none_context_is_clean(self):
        from tracker.context_mixer import build_system_prompt, CLEAN_SYSTEM_PROMPT
        self.assertEqual(build_system_prompt(None), CLEAN_SYSTEM_PROMPT)

    def test_truncation_marker(self):
        from tracker.context_mixer import FileContext, build_system_prompt
        big = FileContext("big.py", "/home/empiric/Projects/EMPIRA_HR/big.py",
                          "x\n" * 50_000, "big.py - EMPIRA_HR")
        self.assertIn("[truncated", build_system_prompt(big))


class TestAudioCapture(unittest.TestCase):
    def _block(self):
        import numpy as np
        return (np.random.randn(1600, 1) * 1000).astype(np.int16)

    def test_wav_export_format(self):
        import wave
        from tracker.audio_capture import AudioRecorder
        rec = AudioRecorder()
        rec._recording = True
        for _ in range(3):
            rec._callback(self._block(), 1600, None, None)
        wav = rec.stop()
        self.assertIsNotNone(wav)
        with wave.open(wav, "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getframerate(), 16000)
            self.assertEqual(wf.getsampwidth(), 2)        # 16-bit
            self.assertEqual(wf.getnframes(), 3 * 1600)

    def test_safety_cap(self):
        from tracker.audio_capture import AudioRecorder
        rec = AudioRecorder(max_seconds=1)               # 16000-frame cap
        rec._recording = True
        for _ in range(40):
            rec._callback(self._block(), 1600, None, None)
        self.assertLessEqual(rec._frame_count, rec._max_frames + 1600)
        rec.stop()

    def test_empty_recording_returns_none(self):
        from tracker.audio_capture import AudioRecorder
        rec = AudioRecorder()
        rec._recording = True
        self.assertIsNone(rec.stop())  # no frames captured

    def test_ptt_state_machine(self):
        import io
        from pynput import keyboard
        from tracker.audio_capture import PushToTalkController

        events = []

        class FakeRec:
            def __init__(self): self._r = False
            @property
            def is_recording(self): return self._r
            def start(self): self._r = True; events.append("start"); return True
            def stop(self): self._r = False; events.append("stop"); return io.BytesIO(b"x")

        fired = []
        ptt = PushToTalkController(on_audio=lambda b: fired.append(b), recorder=FakeRec())
        ptt._on_press(keyboard.Key.ctrl_l)
        ptt._on_press(keyboard.Key.space)
        ptt._on_press(keyboard.Key.space)       # auto-repeat ignored
        ptt._on_release(keyboard.Key.space)
        self.assertEqual(events, ["start", "stop"])
        self.assertEqual(len(fired), 1)


class TestSttEngine(unittest.TestCase):
    def test_empty_buffer_returns_empty_string(self):
        import io
        from tracker.stt_engine import WhisperTranscriber
        t = WhisperTranscriber()
        self.assertEqual(t.transcribe(io.BytesIO(b"")), "")  # decode fails -> ""

    def test_none_input_returns_empty(self):
        from tracker.stt_engine import WhisperTranscriber
        self.assertEqual(WhisperTranscriber().transcribe(None), "")

    def test_segments_are_concatenated(self):
        import io
        from unittest import mock
        from tracker.stt_engine import WhisperTranscriber

        Seg = lambda txt: mock.Mock(text=txt)
        fake_model = mock.Mock()
        fake_model.transcribe.return_value = (
            [Seg(" Fix the "), Seg("divide function. ")],
            mock.Mock(duration=1.5),
        )
        t = WhisperTranscriber()
        t._model = fake_model  # inject so no real model is loaded
        out = t.transcribe(io.BytesIO(b"RIFFfake"))
        self.assertEqual(out, "Fix the divide function.")
        # VAD must be enabled per the spec.
        _, kwargs = fake_model.transcribe.call_args
        self.assertTrue(kwargs.get("vad_filter"))

    def test_config_cpu_optimizations(self):
        from tracker import config
        self.assertEqual(config.STT_MODEL_SIZE, "base.en")
        self.assertEqual(config.STT_DEVICE, "cpu")
        self.assertEqual(config.STT_COMPUTE_TYPE, "int8")
        self.assertEqual(config.STT_CPU_THREADS, 4)


class TestTtsParser(unittest.TestCase):
    def _run(self, tokens):
        from tracker.tts_engine import SentenceBuffer
        spoken, codes = [], []
        buf = SentenceBuffer(emit=spoken.append, emit_code=codes.append)
        for t in tokens:
            buf.feed(t)
        buf.flush()
        return spoken, codes

    def test_sentences_split_on_punctuation(self):
        spoken, _ = self._run(["Hello ", "there. ", "How ", "are you?"])
        self.assertEqual(spoken, ["Hello there.", "How are you?"])

    def test_markdown_stripped(self):
        from tracker.tts_engine import clean_markdown
        self.assertEqual(clean_markdown("**bold** and `code`"), "bold and code")
        self.assertEqual(clean_markdown("[docs](http://x)"), "docs")
        self.assertEqual(clean_markdown("# Title"), "Title")

    def test_code_block_summarized_not_spoken(self):
        spoken, codes = self._run(
            ["Do this:\n", "```python\n", "def f():\n", "    pass\n", "```\n", "Done."]
        )
        self.assertTrue(codes and codes[0] >= 1)
        self.assertFalse(any("def f" in s for s in spoken))
        self.assertIn("Done.", spoken)

    def test_interrupt_bumps_generation_and_drains(self):
        import queue, threading
        from tracker.tts_engine import TTSEngine, SentenceBuffer
        eng = TTSEngine.__new__(TTSEngine)
        eng._text_q, eng._audio_q = queue.Queue(), queue.Queue()
        eng._gen, eng._gen_lock = 0, threading.Lock()
        eng._buffer = SentenceBuffer(lambda s: None, lambda n: None)
        eng._on_sentence("hi.")
        self.assertEqual(eng._text_q.qsize(), 1)
        eng.interrupt()
        self.assertEqual(eng._current_gen(), 1)
        self.assertTrue(eng._text_q.empty() and eng._audio_q.empty())


class TestOrchestrator(unittest.TestCase):
    def test_persona_and_screen_context_prompt(self):
        from unittest import mock
        from tracker import orchestrator as O
        from tracker.context_mixer import FileContext
        ext = FileContext("auth.py", "/home/empiric/Projects/EMPIRA_HR/api/auth.py",
                          "def login():\n    return True\n", "auth.py - EMPIRA_HR")
        orch = O.Orchestrator()
        with mock.patch.object(O, "load_active_context", return_value=ext):
            sysp = orch._build_system_prompt()
        self.assertIn("Nexus Ten", sysp)
        self.assertIn("def login", sysp)
        self.assertIn("auth.py", sysp)

    def test_own_file_excluded_persona_only(self):
        from unittest import mock
        from tracker import orchestrator as O
        from tracker.context_mixer import FileContext
        own = FileContext("db.py", "/home/empiric/Projects/Nexus AI/tracker/db.py",
                          "x=1", "db.py - Nexus AI")
        from tracker import config
        orch = O.Orchestrator()
        with mock.patch.object(O, "load_active_context", return_value=own), \
             mock.patch.object(config, "EXCLUDE_SELF_CONTEXT", True):
            self.assertEqual(orch._build_system_prompt(), O.PERSONA)

    def test_message_history_ordering(self):
        from tracker import orchestrator as O
        orch = O.Orchestrator()
        orch.history.append({"user": "q1", "assistant": "a1"})
        orch.history.append({"user": "q2", "assistant": "a2"})
        msgs = orch._build_messages("SYS", "q3")
        self.assertEqual([m["role"] for m in msgs],
                         ["system", "user", "assistant", "user", "assistant", "user"])
        self.assertEqual(msgs[0]["content"], "SYS")
        self.assertEqual(msgs[-1]["content"], "q3")

    def test_history_capped(self):
        from tracker import orchestrator as O
        from tracker import config
        orch = O.Orchestrator()
        for i in range(config.CONV_HISTORY_TURNS + 3):
            orch.history.append({"user": f"q{i}", "assistant": f"a{i}"})
        self.assertEqual(len(orch.history), config.CONV_HISTORY_TURNS)

    def test_barge_in_cancels_turn_and_interrupts_tts(self):
        import asyncio
        from unittest import mock
        from tracker import orchestrator as O

        async def scenario():
            orch = O.Orchestrator()
            orch.tts = mock.Mock()
            orch._loop = asyncio.get_running_loop()
            orch._events = asyncio.Queue()
            orch.state = O.State.SPEAKING

            async def long_turn():
                await asyncio.sleep(10)

            orch._active_task = asyncio.create_task(long_turn())
            await asyncio.sleep(0.02)
            await orch._barge_in()
            return orch

        orch = asyncio.run(scenario())
        self.assertTrue(orch.tts.interrupt.called)
        self.assertIsNone(orch._active_task)


class TestContextEngine(unittest.TestCase):
    def _seed(self, d):
        from tracker.db import ActivityStore
        store = ActivityStore(Path(d) / "local_logs.db")
        for app, title in [("code", "a.py - EMPIRA_HR - Code"),
                           ("chrome", "Docs - Chrome"),
                           ("code", "b.py - EMPIRA_HR - Code")]:
            store.log(type("S", (), {"app_name": app, "title": title, "pid": 1})())
        store.save_file_context(window_title="b.py - EMPIRA_HR - Code", app_name="code",
                                file_name="b.py", absolute_path="/home/empiric/Projects/EMPIRA_HR/b.py",
                                file_content="x = 1\n")
        store.close()
        return Path(d) / "local_logs.db"

    def test_assemble_and_master_prompt(self):
        from tracker import context_engine as ce
        with tempfile.TemporaryDirectory() as d:
            db = self._seed(d)
            ctx = ce.assemble_context(db_path=db)
            self.assertEqual(ctx.active_file, "b.py")
            self.assertTrue(any("a.py" in l for l in ctx.recent_logs))
            p = ce.build_master_prompt("what now?", ctx)
            self.assertIn("Nexus Ten", p)
            self.assertIn("[LIVE SCREEN]", p)
            self.assertIn("[RECENT HISTORY]", p)
            self.assertIn("[USER SPOKE]: what now?", p)
            self.assertIn("x = 1", p)

    def test_self_referential_screen_hidden(self):
        from unittest import mock
        from tracker.db import ActivityStore
        from tracker import context_engine as ce
        from tracker import config
        with tempfile.TemporaryDirectory() as d:
            store = ActivityStore(Path(d) / "l.db")
            store.save_file_context(window_title="db.py - Nexus AI - Code", app_name="code",
                                    file_name="db.py",
                                    absolute_path="/home/empiric/Projects/Nexus AI/tracker/db.py",
                                    file_content="secret = 1\n")
            store.close()
            with mock.patch.object(config, "EXCLUDE_SELF_CONTEXT", True):
                ctx = ce.assemble_context(db_path=Path(d) / "l.db")
            self.assertIsNone(ctx.active_file)         # own code not exposed
            self.assertIsNone(ctx.file_content)


class TestUIBridge(unittest.TestCase):
    def test_command_in_state_out(self):
        import asyncio, json
        import websockets
        from tracker.ui_bridge import UIBridge

        async def scenario():
            got = []
            async def on_cmd(m): got.append(m)
            bridge = UIBridge(on_command=on_cmd, host="127.0.0.1", port=8773)
            await bridge.start()
            async with websockets.connect("ws://127.0.0.1:8773") as ws:
                await ws.recv()  # initial state
                await ws.send(json.dumps({"cmd": "activate"}))
                await asyncio.sleep(0.05)
                await bridge.emit_state("thinking", "x")
                evt = json.loads(await ws.recv())
            await bridge.stop()
            return got, evt

        got, evt = asyncio.run(scenario())
        self.assertEqual(got, [{"cmd": "activate"}])
        self.assertEqual(evt["state"], "thinking")


class TestVoiceFrontend(unittest.TestCase):
    def _frame(self, rms):
        import numpy as np
        from tracker import config
        return (np.ones(config.WAKE_FRAME_SAMPLES) * rms).astype("int16").reshape(-1, 1)

    def _vf(self, results):
        from tracker.voice_frontend import VoiceFrontend
        vf = VoiceFrontend(
            on_wake=lambda: results.append("wake"),
            on_utterance=lambda w: results.append("utt" if w else "timeout"),
            on_bargein=lambda: results.append("barge"),
        )
        vf._oww = object()  # skip model load
        return vf

    def test_vad_endpoints_utterance(self):
        from tracker.voice_frontend import MODE_LISTEN
        r = []
        vf = self._vf(r); vf.set_mode(MODE_LISTEN)
        for _ in range(15): vf._callback(self._frame(2000), 1280, None, None)
        for _ in range(15): vf._callback(self._frame(0), 1280, None, None)
        self.assertIn("utt", r)

    def test_idle_timeout(self):
        from tracker.voice_frontend import MODE_LISTEN
        from tracker import config
        r = []
        vf = self._vf(r); vf.set_mode(MODE_LISTEN)
        for _ in range(int(config.SESSION_IDLE_TIMEOUT_SEC * vf._fps) + 2):
            vf._callback(self._frame(0), 1280, None, None)
        self.assertEqual(r, ["timeout"])

    def test_bargein_requires_sustained_voice(self):
        from tracker.voice_frontend import MODE_GUARD
        from tracker import config
        r = []
        vf = self._vf(r); vf.set_mode(MODE_GUARD)
        for _ in range(2): vf._callback(self._frame(2000), 1280, None, None)
        vf._callback(self._frame(0), 1280, None, None)
        self.assertEqual(r, [])  # brief blip ignored
        vf.set_mode(MODE_GUARD)
        for _ in range(int(config.BARGEIN_SUSTAIN_MS / 1000 * vf._fps) + 2):
            vf._callback(self._frame(2000), 1280, None, None)
        self.assertEqual(r, ["barge"])


class TestSessionPrompt(unittest.TestCase):
    def test_session_prompt_has_all_blocks(self):
        from tracker.context_engine import build_session_prompt, OmniContext
        ctx = OmniContext("api.py", "api.py - X", "def ping(): return 1\n",
                          ["api.py (code, 09:00)"])
        p = build_session_prompt("what does it return", ctx,
                                 "User: explain\nNexus: a ping endpoint")
        for block in ("[LIVE SCREEN]", "[RECENT HISTORY]", "[CONVERSATION THREAD]",
                      "[USER SPOKE]: what does it return"):
            self.assertIn(block, p)
        self.assertIn("def ping", p)
        self.assertIn("ping endpoint", p)


class TestSituationalPrompt(unittest.TestCase):
    def test_system_prompt_has_persona_and_rules(self):
        from tracker.context_engine import NEXUS_SYSTEM_PROMPT as p
        # Persona anti-robot rules.
        self.assertIn("As an AI", p)            # it's the forbidden phrase list
        self.assertIn("Nexus", p)
        # Conditional-context rules.
        low = p.lower()
        self.assertIn("casual", low)
        self.assertIn("ignore all context", low)        # casual → no context
        self.assertIn("active screen context", low)      # authoritative open file

    def test_context_block_has_screen_and_question(self):
        from tracker.context_engine import build_session_context_block, OmniContext
        ctx = OmniContext("a.py", "a.py - X", "code here\n", ["a.py (code, 09:00)"])
        block = build_session_context_block("explain this", ctx, "")
        self.assertIn("[LIVE SCREEN]", block)
        self.assertIn("code here", block)
        self.assertIn("[USER SPOKE]: explain this", block)

    def test_orchestrator_splits_system_and_user(self):
        from unittest import mock
        from tracker import session_orchestrator as S
        from tracker.context_engine import NEXUS_SYSTEM_PROMPT
        orch = S.SessionOrchestrator()
        msgs = orch._build_messages("[LIVE SCREEN]: x\n[USER SPOKE]: hi")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], NEXUS_SYSTEM_PROMPT)
        self.assertEqual(msgs[1]["role"], "user")


class TestSessionMachine(unittest.TestCase):
    def _orch(self, loop):
        from unittest import mock
        from tracker import session_orchestrator as S
        orch = S.SessionOrchestrator()
        orch.tts = mock.Mock(); orch.tts.is_speaking = False
        orch.voice = mock.Mock(); orch.bridge = mock.Mock()
        async def emit(*a, **k): pass
        orch.bridge.emit_state = emit
        orch.bridge.emit = emit
        orch._loop = loop
        orch._armed = True          # a turn only runs when the agent is armed
        return orch, S

    def test_multiturn_returns_to_listening_with_memory(self):
        import asyncio, io
        from unittest import mock

        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)

            async def fake_gather(q): return "P"
            orch._gather_context = fake_gather
            with mock.patch.object(S, "transcribe_audio", return_value="explain this"), \
                 mock.patch.object(orch, "_stream_to_tts", return_value="A ping endpoint."):
                orch.state = S.State.ACTIVE_LISTENING
                await orch._handle_turn(io.BytesIO(b"w"))
            return orch, S

        orch, S = asyncio.run(scenario())
        self.assertEqual(orch.state, S.State.ACTIVE_LISTENING)
        self.assertEqual(len(orch.memory), 2)

    def test_end_phrase_clears_memory_to_standby(self):
        import asyncio, io
        from unittest import mock

        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)
            orch.memory.append({"role": "user", "content": "hi"})
            with mock.patch.object(S, "transcribe_audio", return_value="ok bye nexus"), \
                 mock.patch.object(orch, "_play_beep"):
                orch.state = S.State.ACTIVE_LISTENING
                await orch._handle_turn(io.BytesIO(b"w"))
            return orch, S

        orch, S = asyncio.run(scenario())
        self.assertEqual(orch.state, S.State.STANDBY)
        self.assertEqual(len(orch.memory), 0)

    def test_two_turns_without_rewake(self):
        import asyncio, io
        from unittest import mock

        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)

            async def fake_gather(q): return "P"
            orch._gather_context = fake_gather
            with mock.patch.object(orch, "_stream_to_tts", side_effect=["A1.", "A2."]):
                orch.state = S.State.ACTIVE_LISTENING
                with mock.patch.object(S, "transcribe_audio", return_value="q1"):
                    await orch._handle_turn(io.BytesIO(b"w"))
                mid = orch.state
                with mock.patch.object(S, "transcribe_audio", return_value="q2"):
                    await orch._handle_turn(io.BytesIO(b"w"))
                return orch, S, mid

        orch, S, mid = asyncio.run(scenario())
        self.assertEqual(mid, S.State.ACTIVE_LISTENING)   # looped back after turn 1
        self.assertEqual(orch.state, S.State.ACTIVE_LISTENING)
        self.assertEqual(len(orch.memory), 4)             # both turns remembered

    def test_interrupt_word_not_answered(self):
        import asyncio, io
        from unittest import mock

        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)
            with mock.patch.object(S, "transcribe_audio", return_value="stop"), \
                 mock.patch.object(orch, "_stream_to_tts") as st:
                orch.state = S.State.ACTIVE_LISTENING
                await orch._handle_turn(io.BytesIO(b"w"))
                return orch, S, st.called

        orch, S, called = asyncio.run(scenario())
        self.assertFalse(called)                          # no LLM answer
        self.assertEqual(orch.state, S.State.ACTIVE_LISTENING)
        self.assertEqual(len(orch.memory), 0)

    def test_silence_stays_awake(self):
        import asyncio
        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)
            orch.state = S.State.ACTIVE_LISTENING
            await orch._on_utterance(None)                # VAD idle timeout
            return orch, S
        orch, S = asyncio.run(scenario())
        self.assertEqual(orch.state, S.State.ACTIVE_LISTENING)   # did not sleep

    def test_is_goodbye_detection(self):
        from tracker.session_orchestrator import SessionOrchestrator as O
        for t in ("Bye", "bye", "bye jarvis", "goodbye", "ok bye", "see you", "sleep nexus"):
            self.assertTrue(O._is_goodbye(t), t)
        for t in ("tell me the classes", "how do I say goodbye to a thread", "what does bye mean"):
            self.assertFalse(O._is_goodbye(t), t)

    def test_goodbye_turns_agent_off(self):
        import asyncio, io
        from unittest import mock
        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)
            orch._armed = True
            orch.memory.append({"role": "user", "content": "x"})
            states = []
            async def emit_state(s, d=""): states.append(s)
            orch.bridge.emit_state = emit_state
            with mock.patch.object(S, "transcribe_audio", return_value="Bye"), \
                 mock.patch.object(orch, "_play_beep"):
                orch.state = S.State.ACTIVE_LISTENING
                await orch._handle_turn(io.BytesIO(b"w"))
            return orch, states
        orch, states = asyncio.run(scenario())
        self.assertFalse(orch._armed)              # agent OFF
        self.assertEqual(states[-1], "off")        # UI toggle off
        self.assertEqual(len(orch.memory), 0)
        self.assertTrue(orch.voice.stop.called)    # wake listener stopped

    def test_bargein_interrupts_to_listening(self):
        import asyncio
        from unittest import mock

        async def scenario():
            loop = asyncio.get_running_loop()
            orch, S = self._orch(loop)
            async def long_turn(): await asyncio.sleep(10)
            orch.state = S.State.SPEAKING
            orch._turn_task = asyncio.create_task(long_turn())
            await asyncio.sleep(0.02)
            await orch._on_bargein()
            return orch, S

        orch, S = asyncio.run(scenario())
        self.assertEqual(orch.state, S.State.ACTIVE_LISTENING)
        self.assertTrue(orch.tts.interrupt.called)


class TestVectorIndexer(unittest.TestCase):
    """Mechanics only — embeddings are mocked so the suite needs no Ollama."""

    def _indexer(self, d):
        from unittest import mock
        from tracker.memory_manager import NexusMemoryManager
        from tracker.vector_indexer import VectorIndexer
        NexusMemoryManager._reset_singleton()
        mem = NexusMemoryManager(persist_dir=Path(d) / "mem")
        idx = VectorIndexer(memory=mem)
        # Deterministic fake embeddings (fixed dim) so Chroma works offline.
        idx._embed = mock.Mock(side_effect=lambda texts: [[0.1, 0.2, 0.3, 0.4]
                                                          for _ in texts])
        return idx

    def test_indexes_only_source_extensions(self):
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            proj = Path(d) / "proj"; (proj / "node_modules").mkdir(parents=True)
            (proj / "a.py").write_text("def f():\n    return 1\n")
            (proj / "b.html").write_text("<div>hi</div>\n")
            (proj / "c.txt").write_text("ignored, wrong ext\n")
            (proj / "node_modules" / "d.py").write_text("ignored, vendored\n")
            res = idx.index_project(str(proj))
            self.assertEqual(res["files"], 2)  # only a.py + b.html

    def test_ignored_and_unknown_return_zero(self):
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            png = Path(d) / "x.png"; png.write_text("nope")
            self.assertEqual(idx.index_single_file(str(png)), 0)

    def test_single_file_update_replaces_chunks(self):
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            f = Path(d) / "m.py"
            f.write_text("def a():\n    return 1\n")
            idx.index_single_file(str(f))
            before = idx.stats()["codebase_chunks"]
            f.write_text("def a():\n    return 2\n")          # edit + re-index
            idx.index_single_file(str(f))
            after = idx.stats()["codebase_chunks"]
            self.assertEqual(before, after)  # replaced, not duplicated

    def test_activity_logs_indexed(self):
        with tempfile.TemporaryDirectory() as d:
            from tracker.db import ActivityStore
            db = Path(d) / "local_logs.db"
            store = ActivityStore(db)
            for app, title in [("code", "x.py - P"), ("chrome", "docs")]:
                store.log(type("S", (), {"app_name": app, "title": title, "pid": 1})())
            store.close()
            idx = self._indexer(d)
            self.assertEqual(idx.index_activity_logs(db_path=db), 2)
            self.assertEqual(idx.stats()["activity_entries"], 2)

    def test_unchanged_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            f = Path(d) / "m.py"
            f.write_text("def a():\n    return 1\n")
            first = idx.index_single_file(str(f))
            self.assertGreater(first, 0)               # indexed
            again = idx.index_single_file(str(f))      # same content
            self.assertEqual(again, 0)                 # skipped (already embedded)

    def test_index_active_file_uses_open_file(self):
        from unittest import mock
        from tracker import vector_indexer as VI
        from tracker.context_mixer import FileContext
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            f = Path(d) / "open.py"; f.write_text("def open_one():\n    return 1\n")
            ext = FileContext("open.py", str(f), "def open_one():\n    return 1\n",
                              "open.py - EMPIRA_HR")
            with mock.patch.object(VI, "load_active_context", return_value=ext):
                n = idx.index_active_file()
            self.assertGreater(n, 0)

    def test_index_active_file_skips_nexus_own_code(self):
        from unittest import mock
        from tracker import vector_indexer as VI
        from tracker.context_mixer import FileContext
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            from tracker import config
            own = FileContext("db.py", "/home/empiric/Projects/Nexus AI/tracker/db.py",
                              "x=1", "db.py - Nexus AI")
            with mock.patch.object(VI, "load_active_context", return_value=own), \
                 mock.patch.object(config, "EXCLUDE_SELF_CONTEXT", True):
                self.assertEqual(idx.index_active_file(), 0)

    def test_python_splitter_keeps_small_function_whole(self):
        with tempfile.TemporaryDirectory() as d:
            idx = self._indexer(d)
            chunks = idx._splitter(".py").split_text(
                "def small():\n    x = 1\n    return x\n")
            self.assertEqual(len(chunks), 1)  # fits in one chunk, not split


class TestRetriever(unittest.TestCase):
    def test_distance_filter_keeps_close_drops_far(self):
        from tracker.retriever import _filter_by_distance
        hits = [{"distance": 0.2, "document": "near"},
                {"distance": 0.9, "document": "far"}]
        kept = _filter_by_distance(hits, max_distance=0.55)
        self.assertEqual([h["document"] for h in kept], ["near"])

    def test_master_prompt_has_all_blocks(self):
        from tracker.retriever import build_master_user_content, HybridContext
        from tracker.context_engine import OmniContext
        ctx = HybridContext(
            active=OmniContext("a.py", "a.py - X", "code x\n", []),
            code_hits=[{"document": "def f(): ...",
                        "metadata": {"file_name": "a.py", "path": "/p/a.py"}}],
            activity_hits=[{"document": "On T, the user did Y."}])
        p = build_master_user_content("explain this", ctx, "User: hi\nNexus: hey")
        for block in ("[ACTIVE SCREEN CONTEXT]", "[OTHER PROJECT FILES]",
                      "[WORK HISTORY]", "[CONVERSATION HISTORY]",
                      "[USER SPOKE]: explain this"):
            self.assertIn(block, p)
        self.assertIn("def f", p)
        self.assertIn("a.py", p)

    def test_casual_question_drops_vector_context(self):
        from tracker.retriever import build_master_user_content, HybridContext
        from tracker.context_engine import OmniContext
        ctx = HybridContext(active=OmniContext(None, None, None, []),
                            code_hits=[], activity_hits=[])
        p = build_master_user_content("how are you?", ctx, "")
        self.assertIn("(none relevant to this question)", p)

    def test_retrieve_context_triple_fetch_and_filter(self):
        import asyncio
        from unittest import mock
        from tracker import retriever as R

        class FakeMem:
            def query_codebase(self, v, k):
                return [{"distance": 0.3, "document": "code",
                         "metadata": {"file_name": "a.py", "path": "/a.py"}}]
            def query_activity(self, v, k):
                return [{"distance": 0.9, "document": "old log", "metadata": {}}]

        async def go():
            with mock.patch.object(R, "embed_query", return_value=[0.0] * 8):
                return await R.retrieve_context("q", FakeMem(), db_path="/nonexistent.db")

        ctx = asyncio.run(go())
        self.assertEqual(len(ctx.code_hits), 1)       # 0.3 kept
        self.assertEqual(len(ctx.activity_hits), 0)   # 0.9 dropped as irrelevant


class TestMemoryManager(unittest.TestCase):
    def _mgr(self, d):
        from tracker.memory_manager import NexusMemoryManager
        NexusMemoryManager._reset_singleton()
        return NexusMemoryManager(persist_dir=Path(d) / "mem")

    @staticmethod
    def _emb(seed):
        return [float((seed * 7 + i) % 13) / 13 for i in range(8)]

    def test_singleton_identity(self):
        with tempfile.TemporaryDirectory() as d:
            from tracker.memory_manager import NexusMemoryManager, get_memory
            m = self._mgr(d)
            self.assertIs(m, NexusMemoryManager(persist_dir=Path("/ignored")))
            self.assertIs(m, get_memory())

    def test_code_upsert_metadata_and_query(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mgr(d)
            m.upsert_code_chunk("/proj/api/auth.py", "def a(): ...", self._emb(1),
                                chunk_index=0, start_line=10, end_line=14)
            hits = m.query_codebase(self._emb(1), n_results=1)
            self.assertTrue(hits[0]["metadata"]["path"].endswith("auth.py"))
            self.assertEqual(hits[0]["metadata"]["start_line"], 10)
            self.assertEqual(hits[0]["metadata"]["end_line"], 14)

    def test_activity_iso_to_unix(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mgr(d)
            m.upsert_activity_log("2026-06-15T09:30:00+00:00", "did X", self._emb(2),
                                  app_name="chrome")
            hits = m.query_activity(self._emb(2), n_results=1)
            self.assertIsInstance(hits[0]["metadata"]["unix_ts"], int)
            self.assertEqual(hits[0]["metadata"]["app_name"], "chrome")

    def test_collections_separate(self):
        with tempfile.TemporaryDirectory() as d:
            m = self._mgr(d)
            m.upsert_code_chunk("/p/a.py", "x", self._emb(1))
            m.upsert_activity_log(1700000000, "log", self._emb(2))
            self.assertEqual(m.stats(), {"codebase_chunks": 1, "activity_entries": 1})

    def test_concurrent_access_no_errors(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            m = self._mgr(d)
            errors = []

            def writer(n):
                try:
                    for i in range(20):
                        m.upsert_code_chunk(f"/p/f{n}.py", f"c{n}{i}", self._emb(n + i),
                                            chunk_index=i)
                except Exception as e:
                    errors.append(e)

            def reader():
                try:
                    for i in range(20):
                        m.query_codebase(self._emb(i), n_results=2)
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=writer, args=(k,)) for k in range(3)] + \
                      [threading.Thread(target=reader) for _ in range(3)]
            for t in threads: t.start()
            for t in threads: t.join()
            self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
