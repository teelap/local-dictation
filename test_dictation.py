"""Regression tests for the pure-logic layers.

Run with: python -m unittest test_dictation -v

These cover the modules that have no OS or UI dependency — the text pipeline,
vocabulary, snippets, stats, and context resolution. Every case here is either a
behaviour taken from the parity spec or a bug that was found and fixed during
the build, so a failure means real, user-visible output changed.
"""

import logging
import shutil
import tempfile
import unittest
from datetime import date, timedelta

import context
import dictionary
import formatter
import injector
import snippets
import stats
from formatter import FormatOptions, format_text

# Several tests deliberately exercise failure paths that log warnings.
logging.disable(logging.CRITICAL)


class FillerAndDisfluencyTests(unittest.TestCase):
    def test_removes_unambiguous_fillers(self):
        result = format_text("um so I was thinking uh maybe we ship it", options=FormatOptions())
        self.assertEqual(result.text, "So I was thinking maybe we ship it.")

    def test_keeps_fillers_when_level_is_off(self):
        options = FormatOptions(filler_level=formatter.FILLER_OFF)
        result = format_text("um so I was thinking", options=options)
        self.assertIn("um", result.text.lower())

    def test_standard_level_removes_discourse_markers(self):
        options = FormatOptions(filler_level=formatter.FILLER_STANDARD)
        result = format_text("you know it's sort of done", options=options)
        self.assertEqual(result.text, "It's done.")

    def test_collapses_stutters(self):
        result = format_text("the the the report is is done", options=FormatOptions())
        self.assertEqual(result.text, "The report is done.")

    def test_preserves_legitimate_repeats(self):
        result = format_text("that had had no effect", options=FormatOptions())
        self.assertIn("had had", result.text)

    def test_digit_runs_are_not_stutters(self):
        """Regression: "five five five" was collapsed, destroying phone numbers."""
        result = format_text("call me at five five five one two one two",
                             options=FormatOptions())
        self.assertEqual(result.text, "Call me at 555-1212.")


class SelfCorrectionTests(unittest.TestCase):
    def test_replaces_noun_phrase(self):
        result = format_text("i think i'll go to the store, no wait, the bank",
                             options=FormatOptions())
        self.assertEqual(result.text, "I think I'll go to the bank.")

    def test_replaces_single_word(self):
        result = format_text("call John, I mean, Jane about it", options=FormatOptions())
        self.assertEqual(result.text, "Call Jane about it.")

    def test_scratch_that_discards_the_clause(self):
        result = format_text("send it to Sarah, scratch that, send it to the team",
                             options=FormatOptions())
        self.assertNotIn("Sarah", result.text)
        self.assertIn("team", result.text)

    def test_leaves_ordinary_sentences_alone(self):
        result = format_text("I actually enjoyed the movie", options=FormatOptions())
        self.assertIn("enjoyed the movie", result.text)


class SpokenSymbolTests(unittest.TestCase):
    def test_email_address(self):
        result = format_text("send it to john at gmail dot com please",
                             options=FormatOptions())
        self.assertEqual(result.text, "Send it to john@gmail.com please.")

    def test_bare_domain(self):
        result = format_text("check out example dot com for the docs",
                             options=FormatOptions())
        self.assertIn("example.com", result.text)

    def test_punctuation_pass_does_not_split_an_email(self):
        """Regression: "john@gmail.com" became "john@gmail. com"."""
        self.assertEqual(formatter.fix_punctuation("email me at john@gmail.com"),
                         "email me at john@gmail.com.")


class NumberTests(unittest.TestCase):
    def test_percent(self):
        result = format_text("revenue grew forty two percent", options=FormatOptions())
        self.assertEqual(result.text, "Revenue grew 42%.")

    def test_currency_with_thousands_separator(self):
        result = format_text("about twenty five thousand dollars", options=FormatOptions())
        self.assertIn("$25,000", result.text)

    def test_small_numbers_stay_words_in_prose(self):
        result = format_text("I have two dogs and three cats", options=FormatOptions())
        self.assertIn("two dogs", result.text)
        self.assertIn("three cats", result.text)

    def test_small_numbers_become_digits_with_a_unit(self):
        result = format_text("give me two hours", options=FormatOptions())
        self.assertIn("2 hours", result.text)

    def test_conjunction_is_not_eaten(self):
        """Regression: "and" inside the number pattern swallowed the conjunction."""
        result = format_text("two dogs and two cats", options=FormatOptions())
        self.assertIn(" and ", result.text)

    def test_compound_with_and(self):
        result = format_text("we need one hundred and five units", options=FormatOptions())
        self.assertIn("105 units", result.text)

    def test_year_keeps_no_separator(self):
        result = format_text("it happened in two thousand twenty six", options=FormatOptions())
        self.assertIn("2026", result.text)
        self.assertNotIn("2,026", result.text)

    def test_version_string(self):
        result = format_text("we shipped version two point one point three",
                             options=FormatOptions())
        self.assertIn("v2.1.3", result.text)

    def test_magnitude_suffix(self):
        result = format_text("the round was fifty k", options=FormatOptions())
        self.assertIn("50K", result.text)


class TimeTests(unittest.TestCase):
    def test_hour_and_minutes(self):
        result = format_text("let's meet at three thirty p m", options=FormatOptions())
        self.assertIn("3:30 PM", result.text)

    def test_leading_zero_minutes(self):
        """Regression: the hour-only pass rewrote "9:05 AM" to "9:5 AM"."""
        result = format_text("standup is at nine oh five a m", options=FormatOptions())
        self.assertIn("9:05 AM", result.text)

    def test_hour_only(self):
        result = format_text("call me at three p m", options=FormatOptions())
        self.assertIn("3 PM", result.text)

    def test_ordinal_date(self):
        result = format_text("on the twenty first", options=FormatOptions())
        self.assertIn("21st", result.text)

    def test_two_word_minutes(self):
        """Regression: the hour group ate "twelve forty", summing the phrase to 57."""
        result = format_text("lunch at twelve forty five p m", options=FormatOptions())
        self.assertIn("12:45 PM", result.text)

    def test_number_run_without_a_time_is_fast(self):
        """Regression: two unbounded number phrases made this cubic."""
        import time

        started = time.perf_counter()
        formatter.apply_spoken_times("twenty five " * 400)
        self.assertLess(time.perf_counter() - started, 1.0)


class CapitalizationTests(unittest.TestCase):
    def test_weekday_is_capitalized(self):
        result = format_text("ship it on friday", options=FormatOptions())
        self.assertIn("Friday", result.text)

    def test_pronoun_i(self):
        result = format_text("i think i'll go", options=FormatOptions())
        self.assertTrue(result.text.startswith("I think I'll"))


class AmbiguityTests(unittest.TestCase):
    """Words that look like something the formatter rewrites, but are not.

    Every case here is a false positive that reached real output during the
    build. They matter more than the features they guard: silently changing a
    word the speaker did say is worse than failing to tidy one.
    """

    def _run(self, text):
        return format_text(text, options=FormatOptions()).text

    def test_modal_may_is_not_the_month(self):
        self.assertIn("we may ship", self._run("we may ship it next week").lower())
        self.assertNotIn("May ship", self._run("we may ship it next week"))

    def test_verb_march_is_not_the_month(self):
        self.assertNotIn("March", self._run("they will march to the office"))

    def test_month_next_to_a_day_number_is_capitalized(self):
        self.assertIn("March 14", self._run("the deadline is march 14"))

    def test_article_before_dot_com_is_not_a_domain(self):
        result = self._run("she works at a dot com company")
        self.assertNotIn("@", result)
        self.assertNotIn("a.com", result)

    def test_initialisms_survive_punctuation_spacing(self):
        self.assertIn("U.S.", self._run("the U.S. economy"))
        self.assertIn("F.B.I.", self._run("call the F.B.I. today"))

    def test_thirty_second_is_a_duration_not_an_ordinal(self):
        self.assertIn("30 second", self._run("a thirty second video"))
        self.assertNotIn("32nd", self._run("a thirty second video"))

    def test_ordinal_after_the_is_still_a_date(self):
        self.assertIn("21st", self._run("on the twenty first"))

    def test_suspended_compound_is_not_a_stutter(self):
        self.assertIn("Pre- and post-launch", self._run("pre- and post-launch reviews"))

    def test_real_cut_off_stutter_is_removed(self):
        self.assertEqual(self._run("I- I think we should go"), "I think we should go.")

    def test_substitution_replacement_is_literal(self):
        """Regression: a backslash or \\1 in a rule raised and killed the dictation."""
        self.assertEqual(
            formatter.apply_substitutions("teh", {"teh": r"the \1 & \\"}),
            r"the \1 & \\")

    def test_llm_guard_keeps_a_genuinely_quoted_sentence(self):
        self.assertEqual(formatter._sanitize_llm_output('"yes."', '"Yes."'), '"Yes."')

    def test_llm_guard_still_strips_added_quotes(self):
        self.assertEqual(formatter._sanitize_llm_output("yes", '"Yes."'), "Yes.")

    def test_trailing_action_keeps_the_previous_period(self):
        self.assertEqual(injector.extract_trailing_action("Done. press enter"),
                         ("Done.", "enter"))


class CleanupLevelTests(unittest.TestCase):
    RAW = "um so i think we should uh ship it on friday at three thirty p m"

    def _run(self, level):
        options = FormatOptions.from_config({"formatting": {"cleanup_level": level}})
        return format_text(self.RAW, options=options).text

    def test_none_is_verbatim(self):
        self.assertEqual(self._run(formatter.CLEANUP_NONE), self.RAW)

    def test_light_keeps_wording_but_drops_fillers(self):
        text = self._run(formatter.CLEANUP_LIGHT)
        self.assertNotIn("um", text.lower().split())
        self.assertIn("three thirty", text)      # numbers untouched at Light

    def test_medium_formats_numbers_and_punctuation(self):
        text = self._run(formatter.CLEANUP_MEDIUM)
        self.assertIn("3:30 PM", text)
        self.assertTrue(text.endswith("."))

    def test_none_ignores_a_profile_that_would_reenable_rewriting(self):
        options = FormatOptions.from_config(
            {"formatting": {"cleanup_level": formatter.CLEANUP_NONE}},
            profile={"auto_capitalize": True, "filler_level": formatter.FILLER_AGGRESSIVE},
        )
        self.assertEqual(format_text(self.RAW, options=options).text, self.RAW)


class LLMGuardrailTests(unittest.TestCase):
    def test_rejects_summarized_output(self):
        source = " ".join(["word"] * 60)
        self.assertIsNone(formatter._sanitize_llm_output(source, "too short"))

    def test_rejects_expanded_output(self):
        self.assertIsNone(
            formatter._sanitize_llm_output("hello", " ".join(["padding"] * 80)))

    def test_strips_code_fences_and_lead_ins(self):
        cleaned = formatter._sanitize_llm_output(
            "hello there friend", "```\nHello there, friend.\n```")
        self.assertEqual(cleaned, "Hello there, friend.")

    def test_strips_chatty_prefix(self):
        cleaned = formatter._sanitize_llm_output(
            "hello there friend", "Here is the corrected text: Hello there, friend.")
        self.assertEqual(cleaned, "Hello there, friend.")

    def test_rejects_an_answer_to_the_transcript(self):
        """A reply can be the same length as the question, so length is not enough."""
        question = "what is the capital of france and why does it matter to us"
        self.assertIsNone(formatter._sanitize_llm_output(
            question, "The capital of France is Paris, a major European hub."))

    def test_rejects_an_obeyed_instruction(self):
        request = "write me a haiku about the ocean please and thank you"
        self.assertIsNone(formatter._sanitize_llm_output(
            request, "Waves crash on the shore, salt spray lingers in the air, "
                     "tides pull at my feet."))

    def test_keeps_a_genuine_cleanup(self):
        raw = "um so i think we should uh ship it on friday because the tests are green"
        self.assertEqual(
            formatter._sanitize_llm_output(
                raw, "I think we should ship it on Friday because the tests are green."),
            "I think we should ship it on Friday because the tests are green.")

    def test_keeps_a_tone_polish(self):
        raw = "hey did you get a chance to look at the doc i sent over yesterday"
        polished = "Hey — did you get a chance to look at the doc I sent over yesterday?"
        self.assertEqual(formatter._sanitize_llm_output(raw, polished), polished)


class TrailingActionTests(unittest.TestCase):
    def test_press_enter_at_end(self):
        self.assertEqual(injector.extract_trailing_action("Sounds good, press enter"),
                         ("Sounds good", "enter"))

    def test_phrase_mid_sentence_is_content(self):
        text = "can you hit enter on that form"
        self.assertEqual(injector.extract_trailing_action(text), (text, None))

    def test_period_between_years_is_not_a_command(self):
        text = "the period between 1990 and 2000 was interesting"
        self.assertEqual(injector.extract_trailing_action(text), (text, None))


class SeamTests(unittest.TestCase):
    def test_continues_mid_sentence(self):
        self.assertEqual(injector.join_with_context("We should ship.", "I was thinking that"),
                         " we should ship.")

    def test_starts_fresh_after_a_period(self):
        self.assertEqual(injector.join_with_context("We should ship.", "Done."),
                         " We should ship.")

    def test_preserves_acronyms(self):
        self.assertEqual(injector.join_with_context("API keys matter.", "the value of "),
                         "API keys matter.")

    def test_preserves_mixed_caps_identifiers(self):
        self.assertEqual(injector.join_with_context("iPhone builds.", "we tested "),
                         "iPhone builds.")


class DictionaryTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        dictionary.init(self.dir)
        dictionary.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_alias_correction(self):
        dictionary.add("Tlapek", sounds_like=["to lapek"])
        self.assertEqual(dictionary.correct("I spoke with to lapek"), "I spoke with Tlapek")

    def test_fuzzy_correction(self):
        dictionary.add("Kubernetes")
        self.assertEqual(dictionary.correct("deploy on kubernetties"), "deploy on Kubernetes")

    def test_hyphenated_term_spans_two_words(self):
        """Regression: multi-word terms only matched single-word windows."""
        dictionary.add("faster-whisper")
        self.assertEqual(dictionary.correct("we use faster whisper"), "we use faster-whisper")

    def test_duplicates_are_rejected(self):
        dictionary.add("OAuth")
        ok, _ = dictionary.add("OAuth")
        self.assertFalse(ok)

    def test_starred_terms_bias_first(self):
        dictionary.add("Zebra")
        dictionary.add("Aardvark", starred=True)
        self.assertEqual(dictionary.bias_terms()[0], "Aardvark")

    def test_corrects_casing_of_a_correctly_spelled_term(self):
        dictionary.add("Kubernetes")
        self.assertEqual(dictionary.correct("deploy on kubernetes"), "deploy on Kubernetes")

    def test_leaves_the_users_own_capitals_alone(self):
        dictionary.add("Kubernetes")
        self.assertEqual(dictionary.correct("KUBERNETES rocks"), "KUBERNETES rocks")

    def test_auto_learn_accepts_a_spelling_fix(self):
        learned = dictionary.learn_from_correction("I emailed Anthropik today",
                                                   "I emailed Anthropic today")
        self.assertEqual(learned, ["Anthropic"])

    def test_auto_learn_ignores_a_rewrite(self):
        learned = dictionary.learn_from_correction("the cat sat on the mat",
                                                   "completely different wording here")
        self.assertEqual(learned, [])

    def test_import_accepts_competitor_shape(self):
        added, _, error = dictionary.import_entries('[{"name":"OAuth"},{"name":"webhook"}]')
        self.assertIsNone(error)
        self.assertEqual(added, 2)


class SnippetTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        snippets.init(self.dir)
        snippets.clear()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_expands_trigger(self):
        snippets.add("my work email", "jacob@tlapek.com")
        text, fired = snippets.expand("send it to my work email please")
        self.assertEqual(text, "send it to jacob@tlapek.com please")
        self.assertEqual(fired, ["my work email"])

    def test_longest_trigger_wins(self):
        snippets.add("my work", "Acme Corp")
        snippets.add("my work email", "jacob@tlapek.com")
        text, _ = snippets.expand("send it to my work email")
        self.assertIn("jacob@tlapek.com", text)
        self.assertNotIn("Acme", text)

    def test_no_trigger_leaves_text_alone(self):
        snippets.add("standup", "Yesterday:")
        text, fired = snippets.expand("nothing to see here")
        self.assertEqual(text, "nothing to see here")
        self.assertEqual(fired, [])

    def test_expansion_containing_another_trigger_does_not_cascade(self):
        """Regression: a signature ending in "sig" expanded the "sig" snippet."""
        snippets.add("my signature block", "Best regards, sig")
        snippets.add("sig", "Jacob Tlapek")
        text, fired = snippets.expand("thanks, my signature block")
        self.assertEqual(text, "thanks, Best regards, sig")
        self.assertEqual(fired, ["my signature block"])

    def test_two_independent_triggers_both_fire(self):
        snippets.add("my work email", "jacob@tlapek.com")
        snippets.add("standup", "Yesterday:")
        text, fired = snippets.expand("my work email and standup")
        self.assertIn("jacob@tlapek.com", text)
        self.assertIn("Yesterday:", text)
        self.assertEqual(len(fired), 2)

    def test_expansion_with_regex_metacharacters_is_literal(self):
        snippets.add("regex snippet", r"cost is $5 (50\% off) [sale]")
        text, _ = snippets.expand("the regex snippet applies")
        self.assertIn(r"cost is $5 (50\% off) [sale]", text)

    def test_expansion_keeps_its_own_indentation(self):
        """Regression: the seam tidy collapsed indentation inside the snippet."""
        snippets.add("standup", "Yesterday:\n  - shipped\nToday:")
        text, _ = snippets.expand("here is the standup")
        self.assertIn("\n  - shipped\n", text)


class PerformanceTests(unittest.TestCase):
    """Guards against the pipeline going superlinear.

    The app supports 20-minute hands-free sessions, so the formatter has to cope
    with several thousand words of run-on speech containing no punctuation for it
    to anchor on. Both of these were quadratic once and stalled for tens of
    seconds on exactly that input.
    """

    SENTENCE = ("um so i think we should uh ship it on friday at three thirty p m "
                "and send the notes to john at gmail dot com about the twenty five "
                "percent increase ")

    def test_formatter_handles_a_long_session(self):
        import time

        text = self.SENTENCE * 300          # ~9,600 words, a full 20-minute session
        started = time.perf_counter()
        format_text(text, options=FormatOptions())
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, 5.0,
                        f"formatting 9,600 words took {elapsed:.1f}s — check for "
                        "an unbounded regex in the self-correction stage")

    def test_dictionary_handles_a_long_session(self):
        import time

        directory = tempfile.mkdtemp()
        try:
            dictionary.init(directory)
            dictionary.clear()
            for index in range(120):
                dictionary.add(f"Term{index}Word")

            text = "we deployed the service and reviewed the metrics today " * 400
            started = time.perf_counter()
            dictionary.correct(text)
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 8.0,
                            f"correcting {len(text.split())} words against 120 terms "
                            f"took {elapsed:.1f}s — check the fuzzy-match pre-filters")
        finally:
            shutil.rmtree(directory, ignore_errors=True)


class ContextTests(unittest.TestCase):
    def test_known_app_maps_to_category(self):
        self.assertEqual(context.categorize("slack.exe", "")[0], context.CAT_WORK)

    def test_browser_resolves_by_title(self):
        self.assertEqual(context.categorize("chrome.exe", "Inbox (12) - Gmail")[0],
                         context.CAT_EMAIL)

    def test_browser_falls_back_to_other(self):
        self.assertEqual(context.categorize("chrome.exe", "Some Random Site")[0],
                         context.CAT_OTHER)

    def test_user_override_wins(self):
        category, source = context.categorize(
            "slack.exe", "", overrides={"slack.exe": context.CAT_PERSONAL})
        self.assertEqual(category, context.CAT_PERSONAL)
        self.assertEqual(source, "override")

    def test_disabled_context_reads_no_window(self):
        resolved = context.resolve({"context": {"enabled": False}})
        self.assertEqual(resolved.source, "disabled")
        self.assertEqual(resolved.title, "")

    def test_technical_style_leaves_identifiers_alone(self):
        ctx = context.AppContext(exe="code.exe", category=context.CAT_CODE,
                                 style=context.STYLE_TECHNICAL)
        options = FormatOptions.from_config({}, context.profile_for(ctx, {}))
        self.assertFalse(options.auto_capitalize)
        self.assertFalse(options.smart_numbers)


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        stats.init(self.dir)
        stats.reset()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _seed(self, offsets):
        today = date.today()
        for offset in offsets:
            key = (today - timedelta(days=offset)).isoformat()
            stats._data["days"][key] = {
                "words": 100, "sessions": 2, "audio_seconds": 60.0,
                "cleaned_words": 10, "replacements": 1, "apps": {"slack.exe": 2},
            }
        stats.save()

    def test_streak_counts_consecutive_days(self):
        self._seed([0, 1, 2, 4])
        self.assertEqual(stats.streak(), 3)

    def test_streak_survives_a_quiet_morning(self):
        self._seed([1, 2, 3])
        self.assertEqual(stats.streak(), 3)

    def test_streak_is_zero_after_a_gap(self):
        self._seed([3, 4, 5])
        self.assertEqual(stats.streak(), 0)

    def test_words_per_minute(self):
        self._seed([0])
        self.assertEqual(stats.words_per_minute(), 100.0)

    def test_time_saved_is_never_negative(self):
        stats.record_session(words=1, audio_seconds=600)
        self.assertGreaterEqual(stats.time_saved_seconds(), 0)

    def test_record_session_accumulates(self):
        stats.record_session(words=50, audio_seconds=30, app="code.exe")
        stats.record_session(words=25, audio_seconds=15, app="code.exe")
        totals = stats.totals()
        self.assertEqual(totals["words"], 75)
        self.assertEqual(totals["sessions"], 2)
        self.assertEqual(totals["apps"]["code.exe"], 2)


try:
    import main as main_module
    _MAIN_IMPORTABLE = True
except Exception:  # noqa: BLE001 — sounddevice/keyboard are absent off Windows
    _MAIN_IMPORTABLE = False


@unittest.skipUnless(_MAIN_IMPORTABLE, "requires the audio and hotkey dependencies")
class HotkeyStateMachineTests(unittest.TestCase):
    """The interleavings that a hotkey library's prefix matching actually produces.

    The default bindings are nested — push-to-talk is Ctrl+Win and command mode
    is Ctrl+Win+Alt — and the shorter combination fires the moment its keys are
    down. Every case here is a state the app has to survive.
    """

    def setUp(self):
        import threading

        self.dir = tempfile.mkdtemp()
        import config_manager
        self.app = main_module.DictationApp.__new__(main_module.DictationApp)
        self.app.config = copy_default_config(config_manager)
        self.app._session_lock = threading.Lock()
        self.app._model_ready = threading.Event()
        self.app._model_ready.set()
        self.app._recording = False
        self.app._processing = False
        self.app._paused = False
        self.app._mode = "dictation"
        self.app._session_started = 0.0
        self.app._latched = False
        self.app._press_time = 0.0
        self.app._last_tap_time = 0.0
        self.app._pending_selection = ""
        self.app._cancelled = False
        self.app.tray = _NullTray()
        self.app.overlay = _NullOverlay()
        self.app.root = None
        self.app.hub = None

    def tearDown(self):
        import audio
        try:
            audio.discard_recording()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_command_mode_survives_the_push_to_talk_prefix(self):
        self.app._on_ptt_press()
        self.app._on_command_mode()
        self.assertTrue(self.app._recording)
        self.assertEqual(self.app._mode, "command")

    def test_hands_free_latches_rather_than_ending_the_prefix_session(self):
        self.app._on_ptt_press()
        self.app._on_hands_free()
        self.assertTrue(self.app._recording)
        self.assertTrue(self.app._latched)

    def test_hands_free_still_stops_a_mature_session(self):
        import time

        self.app._on_ptt_press()
        self.app._session_started = time.monotonic() - 5.0
        self.app._on_hands_free()
        self.assertFalse(self.app._recording)

    def test_double_tap_latches_and_survives_release(self):
        import time

        self.app._on_ptt_press()
        self.app._press_time = time.monotonic() - 0.01
        self.app._on_ptt_release()               # too short — discarded
        self.assertFalse(self.app._recording)

        self.app._on_ptt_press()                 # second press inside the window
        self.assertTrue(self.app._latched)
        self.app._on_ptt_release()               # must not end a latched session
        self.assertTrue(self.app._recording)

    def test_microphone_failure_clears_the_latch(self):
        import audio

        original = audio.start_recording
        audio.start_recording = lambda **_kwargs: False
        try:
            self.app._on_hands_free()
        finally:
            audio.start_recording = original

        self.assertFalse(self.app._latched)
        self.assertFalse(self.app._recording)

    def test_cancel_during_transcription_suppresses_output(self):
        self.app._processing = True
        self.app._recording = False
        self.app.cancel()
        self.assertTrue(self.app._cancelled)


def copy_default_config(config_manager):
    import copy

    return copy.deepcopy(config_manager.DEFAULT_CONFIG)


class _NullTray:
    def notify(self, *args, **kwargs):
        pass

    def set_state(self, state):
        pass


class _NullOverlay:
    def show_recording(self):
        pass

    def show_command(self):
        pass

    def show_transcribing(self):
        pass

    def hide(self):
        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
