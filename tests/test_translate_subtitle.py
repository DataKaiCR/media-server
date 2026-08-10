from dataclasses import replace
import json
import os
from pathlib import Path
from unittest import mock
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from subtitle_translation.errors import TranslationError, ValidationError
import subtitle_translation.ollama as ollama_module
import subtitle_translation.srt as srt_module
from subtitle_translation.ollama import (
    OllamaClient,
    cue_payload,
    make_chunks,
    translate_cues,
)
from subtitle_translation.publication import file_sha256, publish_translation
import subtitle_translation.publication as publication
from subtitle_translation.srt import parse_srt, parse_srt_bytes, render_srt
from subtitle_translation.validation import (
    parse_translation_response,
    validate_source_language,
    validate_translation_quality,
)


VALID_SRT = b"""1
00:00:01,000 --> 00:00:02,000
Hello, how are you?

2
00:00:02,500 --> 00:00:04,000
I am fine, thank you.
"""


class SrtValidationTest(unittest.TestCase):
    def test_parses_utf8_and_preserves_timestamp_lines(self) -> None:
        cues = parse_srt_bytes(b"\xef\xbb\xbf" + VALID_SRT, runtime_seconds=5)
        self.assertEqual([cue.index for cue in cues], [1, 2])
        self.assertEqual(cues[0].timestamp, "00:00:01,000 --> 00:00:02,000")
        self.assertEqual(render_srt(cues), VALID_SRT)

    def test_rejects_invalid_encoding_and_empty_content(self) -> None:
        with self.assertRaisesRegex(ValidationError, "UTF-8"):
            parse_srt_bytes(b"1\n00:00:00,000 --> 00:00:01,000\n\xff\n")
        with self.assertRaisesRegex(ValidationError, "empty"):
            parse_srt_bytes(b"")

    def test_rejects_duplicate_indexes_overlaps_and_runtime_overrun(self) -> None:
        duplicate = VALID_SRT.replace(b"\n2\n", b"\n1\n")
        with self.assertRaisesRegex(ValidationError, "duplicated"):
            parse_srt_bytes(duplicate)
        overlap = VALID_SRT.replace(b"00:00:02,500", b"00:00:01,500")
        with self.assertRaisesRegex(ValidationError, "overlap"):
            parse_srt_bytes(overlap)
        with self.assertRaisesRegex(ValidationError, "runtime"):
            parse_srt_bytes(VALID_SRT, runtime_seconds=3)

    def test_rejects_malformed_and_regressing_timestamps(self) -> None:
        malformed = VALID_SRT.replace(b"00:00:01,000", b"00:00:61,000")
        with self.assertRaisesRegex(ValidationError, "component"):
            parse_srt_bytes(malformed)
        regressing = VALID_SRT.replace(
            b"00:00:02,500 --> 00:00:04,000",
            b"00:00:00,100 --> 00:00:00,900",
        )
        with self.assertRaisesRegex(ValidationError, "regress"):
            parse_srt_bytes(regressing)

    def test_rejects_a_feature_length_subtitle_truncated_before_midpoint(self) -> None:
        with self.assertRaisesRegex(ValidationError, "truncated"):
            parse_srt_bytes(VALID_SRT, runtime_seconds=3600)

    def test_exact_timestamp_and_runtime_boundaries_are_not_off_by_one(self) -> None:
        touching = VALID_SRT.replace(b"00:00:02,500", b"00:00:02,000")
        cues = parse_srt_bytes(touching, runtime_seconds=4)
        self.assertEqual(len(cues), 2)
        invalid_component = VALID_SRT.replace(
            b"00:00:01,000", b"00:00:60,000"
        )
        with self.assertRaisesRegex(ValidationError, "component"):
            parse_srt_bytes(invalid_component)

    def test_rejects_nonfinite_runtime_oversized_input_and_numeric_abuse(self) -> None:
        for runtime in (float("nan"), float("inf"), 0, -1, True):
            with self.subTest(runtime=runtime):
                with self.assertRaisesRegex(ValidationError, "finite and positive"):
                    parse_srt_bytes(VALID_SRT, runtime_seconds=runtime)
        with mock.patch.object(srt_module, "MAX_SRT_BYTES", 64):
            with self.assertRaisesRegex(ValidationError, "safe input size"):
                parse_srt_bytes(VALID_SRT)
        long_text = VALID_SRT.replace(
            b"Hello, how are you?", b"x" * 21
        )
        with mock.patch.object(srt_module, "MAX_CUE_TEXT_CHARS", 20):
            with self.assertRaisesRegex(ValidationError, "safe text size"):
                parse_srt_bytes(long_text)
        huge_index = VALID_SRT.replace(b"1\n", b"1234567890\n", 1)
        with self.assertRaisesRegex(ValidationError, "invalid index"):
            parse_srt_bytes(huge_index)

    def test_parse_srt_refuses_a_symlink_without_reading_its_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "private.srt"
            target.write_bytes(VALID_SRT)
            link = root / "linked.srt"
            link.symlink_to(target)
            with self.assertRaisesRegex(ValidationError, "symlink"):
                parse_srt(link)


class ChunkAndResponseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = parse_srt_bytes(VALID_SRT)

    def test_chunks_have_context_without_retranslating_overlap(self) -> None:
        chunks = make_chunks(self.cues, max_cues=1, max_chars=100, context_cues=1)
        self.assertEqual(len(chunks), 2)
        self.assertEqual([cue.index for cue in chunks[0].cues], [1])
        self.assertEqual([cue.index for cue in chunks[0].context_after], [2])
        self.assertEqual([cue.index for cue in chunks[1].context_before], [1])

    def test_strips_only_deterministic_outer_formatting_from_model_input(self) -> None:
        outer = replace(self.cues[0], text="<i>Hello</i>")
        inner = replace(self.cues[0], text="Hello, <i>friend</i>")
        self.assertEqual(cue_payload(outer)["text"], "Hello")
        self.assertEqual(cue_payload(inner)["text"], inner.text)

    def test_protects_and_restores_proper_names(self) -> None:
        named = replace(self.cues[0], text="Look, Marisol, and Willow Creek.")
        self.assertEqual(
            cue_payload(named)["text"],
            "Look, __PN0__, and __PN1__.",
        )
        response = json.dumps(
            {"translations": [{"id": 1, "text": "Mira, __PN0__, y __PN1__."}]}
        )
        self.assertEqual(
            parse_translation_response(response, (named,)),
            ["Mira, Marisol, y Willow Creek."],
        )
        missing = json.dumps(
            {"translations": [{"id": 1, "text": "Mira, Maribel, y Arroyo Sauce."}]}
        )
        with self.assertRaisesRegex(ValidationError, "proper name"):
            parse_translation_response(missing, (named,))

    def test_accepts_ordered_json_and_rejects_cue_drift(self) -> None:
        content = json.dumps(
            {"translations": [{"id": 1, "text": "Hola, ¿cómo estás?"}, {"id": 2, "text": "Estoy bien, gracias."}]}
        )
        result = parse_translation_response(content, tuple(self.cues))
        self.assertEqual(result[0], "Hola, ¿cómo estás?")
        wrong_order = json.dumps(
            {"translations": [{"id": 2, "text": "Hola"}, {"id": 1, "text": "Bien"}]}
        )
        with self.assertRaisesRegex(ValidationError, "reordered"):
            parse_translation_response(wrong_order, tuple(self.cues))

    def test_rejects_castilian_timestamp_and_formatting_changes(self) -> None:
        vosotros = json.dumps(
            {"translations": [{"id": 1, "text": "Vosotros estáis bien"}, {"id": 2, "text": "Estoy bien"}]}
        )
        with self.assertRaisesRegex(ValidationError, "Castilian"):
            parse_translation_response(vosotros, tuple(self.cues))
        timestamp = json.dumps(
            {"translations": [{"id": 1, "text": "00:00:01,000 Hola"}, {"id": 2, "text": "Bien"}]}
        )
        with self.assertRaisesRegex(ValidationError, "timestamp"):
            parse_translation_response(timestamp, tuple(self.cues))

        tagged = replace(self.cues[0], text="<i>Hello</i>")
        missing_outer_tag = json.dumps({"translations": [{"id": 1, "text": "Hola"}]})
        self.assertEqual(
            parse_translation_response(missing_outer_tag, (tagged,)),
            ["<i>Hola</i>"],
        )
        inner_tagged = replace(self.cues[0], text="Hello, <i>friend</i>")
        with self.assertRaisesRegex(ValidationError, "formatting"):
            parse_translation_response(missing_outer_tag, (inner_tagged,))


class QualityAndRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.cues = parse_srt_bytes(VALID_SRT)
        self.spanish = [
            replace(self.cues[0], text="Hola, ¿cómo estás?"),
            replace(self.cues[1], text="Estoy bien, gracias."),
        ]

    def test_rejects_non_english_source(self) -> None:
        spanish = [
            replace(
                self.cues[0],
                index=index,
                text="Ella está aquí porque tiene algo que decir, pero no sabe cómo hacerlo.",
            )
            for index in range(1, 21)
        ]
        with self.assertRaisesRegex(ValidationError, "English"):
            validate_source_language(spanish)

    def test_rejects_untranslated_and_truncated_output(self) -> None:
        with self.assertRaisesRegex(ValidationError, "untranslated"):
            validate_translation_quality(self.cues, self.cues)
        truncated = [replace(cue, text="a") for cue in self.cues]
        with self.assertRaisesRegex(ValidationError, "truncated"):
            validate_translation_quality(self.cues, truncated)

    def test_retries_deterministically_after_invalid_response(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.calls = []

            def translate_chunk(self, model, chunk, seed, keep_alive, prior_error):
                self.calls.append((seed, prior_error))
                if len(self.calls) == 1:
                    raise ValidationError("changed cue count")
                return ["Hola, ¿cómo estás?", "Estoy bien, gracias."], {
                    "output_tokens": 8
                }

        client = FakeClient()
        translated, metrics = translate_cues(
            client,
            "model",
            self.cues,
            max_cues=2,
            max_chars=100,
            context_cues=1,
            retries=2,
            retry_delay=0,
            seed=7,
            keep_alive="1m",
        )
        self.assertEqual([call[0] for call in client.calls], [7, 8])
        self.assertIn("changed cue count", client.calls[1][1])
        self.assertEqual(translated, self.spanish)
        self.assertEqual(metrics[0]["attempt"], 2)

    def test_exact_translation_length_boundary_is_accepted(self) -> None:
        source = [replace(self.cues[0], text="abcdefghijklmnopqrst")]
        translated = [replace(source[0], text="abcdefghi")]
        self.assertIsNone(validate_translation_quality(source, translated))

    def test_rejects_invalid_retry_controls_before_calling_the_client(self) -> None:
        class NeverCalled:
            def translate_chunk(self, *args):
                raise AssertionError("client must not be called")

        for retries, delay in ((0, 0), (-1, 0), (1, -1), (1, float("nan"))):
            with self.subTest(retries=retries, delay=delay):
                with self.assertRaisesRegex(ValueError, "retry settings"):
                    translate_cues(
                        NeverCalled(), "model", self.cues,
                        max_cues=2, max_chars=100, context_cues=0,
                        retries=retries, retry_delay=delay, seed=0,
                        keep_alive="1m",
                    )

    def test_raises_after_retry_budget_is_exhausted(self) -> None:
        class FailingClient:
            def translate_chunk(self, *args):
                raise TranslationError("offline")

        with self.assertRaisesRegex(TranslationError, "after 2 attempts"):
            translate_cues(
                FailingClient(), "model", self.cues,
                max_cues=2, max_chars=100, context_cues=0,
                retries=2, retry_delay=0, seed=0, keep_alive="1m",
            )


class AtomicPublicationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "movie.en.srt"
        self.destination = self.root / "movie.es-MX.generated.srt"
        self.manifest = self.root / "private" / "translations.jsonl"
        self.backups = self.root / "backups"
        self.source.write_bytes(VALID_SRT)
        self.source_cues = parse_srt(self.source)
        self.translated = [
            replace(self.source_cues[0], text="Hola, ¿cómo estás?"),
            replace(self.source_cues[1], text="Estoy bien, gracias."),
        ]

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish(
        self,
        replace_existing: bool = False,
        expected_source_hash: str | None = None,
        destination: Path | None = None,
    ):
        return publish_translation(
            source_path=self.source,
            expected_source_hash=expected_source_hash or file_sha256(self.source),
            source_cues=self.source_cues,
            translated_cues=self.translated,
            destination=destination or self.destination,
            replace_existing=replace_existing,
            backup_dir=self.backups if replace_existing else None,
            manifest=self.manifest,
            model="translategemma:12b",
            model_digest="a" * 64,
            prompt_version="test-v1",
            prompt_sha256="b" * 64,
            runtime_seconds=5,
            metrics=[{"chunk": 1, "attempt": 1}],
        )

    def test_publishes_atomically_with_xattrs_and_manifest(self) -> None:
        record = self.publish()
        self.assertEqual(parse_srt(self.destination), self.translated)
        self.assertEqual(os.getxattr(self.destination, "user.media_server.generated"), b"true")
        self.assertEqual(
            os.getxattr(self.destination, "user.media_server.subtitle_source"),
            b"ollama-translation",
        )
        rows = [json.loads(line) for line in self.manifest.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sha256"], record["sha256"])
        self.assertEqual(rows[0]["cue_count"], 2)
        self.assertEqual(rows[0]["prompt_version"], "test-v1")
        self.assertEqual(rows[0]["prompt_sha256"], "b" * 64)
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)

    def test_refuses_to_replace_a_human_or_unmarked_subtitle(self) -> None:
        self.destination.write_text("human subtitle", encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "not authorized"):
            self.publish(replace_existing=False)
        with self.assertRaisesRegex(ValidationError, "human"):
            self.publish(replace_existing=True)

    def test_backs_up_an_authorized_generated_replacement(self) -> None:
        original = b"old generated subtitle\n"
        self.destination.write_bytes(original)
        os.setxattr(self.destination, "user.media_server.generated", b"true")
        os.setxattr(
            self.destination,
            "user.media_server.subtitle_source",
            b"ollama-translation",
        )
        record = self.publish(replace_existing=True)
        backup = Path(record["backup_path"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_rolls_back_when_manifest_publication_fails(self) -> None:
        original = b"existing destination\n"
        self.destination.write_bytes(original)
        os.setxattr(self.destination, "user.media_server.generated", b"true")
        os.setxattr(
            self.destination,
            "user.media_server.subtitle_source",
            b"ollama-translation",
        )
        with mock.patch.object(publication, "append_manifest", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.publish(replace_existing=True)
        self.assertEqual(self.destination.read_bytes(), original)

    def test_rejects_a_source_changed_during_translation(self) -> None:
        with self.assertRaisesRegex(ValidationError, "changed during"):
            self.publish(expected_source_hash="0" * 64)
        self.assertFalse(self.destination.exists())

    def test_failed_new_publication_removes_destination(self) -> None:
        with mock.patch.object(publication, "append_manifest", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertFalse(self.destination.exists())

    def test_same_file_and_symlink_destinations_are_refused_unchanged(self) -> None:
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValidationError, "must differ"):
            self.publish(destination=self.source)
        target = self.root / "target.srt"
        target.write_bytes(b"human target\n")
        link = self.root / "linked.srt"
        link.symlink_to(target)
        with self.assertRaisesRegex(ValidationError, "symlinks"):
            self.publish(destination=link)
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(target.read_bytes(), b"human target\n")


class OllamaTransportTest(unittest.TestCase):
    class Response:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, maximum: int = -1) -> bytes:
            return self.payload if maximum < 0 else self.payload[:maximum]

    def test_client_requires_a_bounded_loopback_origin_and_timeout(self) -> None:
        self.assertEqual(
            OllamaClient("http://[::1]:11434/").base_url,
            "http://[::1]:11434",
        )
        for url in (
            "https://example.invalid",
            "http://user:password@127.0.0.1:11434",
            "http://127.0.0.1:11434/private",
            "http://127.0.0.1:bad",
        ):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "Ollama URL"):
                    OllamaClient(url)
        for timeout in (0, -1, float("nan"), float("inf"), True, 3601):
            with self.subTest(timeout=timeout):
                with self.assertRaisesRegex(ValueError, "timeout"):
                    OllamaClient("http://127.0.0.1:11434", timeout)

    def test_model_inventory_is_bounded_and_strictly_shaped(self) -> None:
        client = OllamaClient("http://127.0.0.1:11434")
        with mock.patch.object(
            ollama_module, "MAX_OLLAMA_RESPONSE_BYTES", 8
        ), mock.patch(
            "subtitle_translation.ollama.urllib.request.urlopen",
            return_value=self.Response(b"x" * 9),
        ):
            with self.assertRaisesRegex(TranslationError, "safety limit"):
                client.model_digest("model")

        malformed = (
            b"\xff",
            b"[]",
            b'{"models":[null]}',
            b'{"models":[{"name":"model","digest":"PRIVATE/DIGEST"}]}',
        )
        for payload in malformed:
            with self.subTest(payload=payload), mock.patch(
                "subtitle_translation.ollama.urllib.request.urlopen",
                return_value=self.Response(payload),
            ):
                with self.assertRaises(TranslationError):
                    client.model_digest("model")

        valid = json.dumps({
            "models": [{"name": "model", "digest": "sha256:" + "a" * 64}]
        }).encode("utf-8")
        with mock.patch(
            "subtitle_translation.ollama.urllib.request.urlopen",
            return_value=self.Response(valid),
        ):
            self.assertEqual(client.model_digest("model"), "sha256:" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
