"""Codec conformance suite (reuse-design stage 4b).

The proposal's conformance-test checklist, now ONE suite over the
engine: every switched primitive's public function must satisfy the
universal behaviors of the KeyedCollectionMerge contract, whatever
its codec does internally —

  - never raises on hostile input (garbage text, hostile obligation
    objects); every path returns a wire result with a valid status;
  - deterministic: identical inputs → identical results;
  - idempotent re-entry: applying to the already-edited text is a
    no-op (no double application);
  - filter contract: only added + non-exclusive obligations are ever
    acted on (status-checking is universal EXCEPT the import
    primitive, whose original filter predates the status field — see
    the deviation note at STATUS_CHECKING);
  - the APPLIED certificate carries the contract keys (primitive id,
    risk_tier A, before/after hashes);
  - transactional honesty: any non-APPLIED status returns the input
    text byte-for-byte.

A new primitive's switch is not complete until it is added here.
"""

from __future__ import annotations

import hashlib

import pytest

from capybase.change_accounting import BranchObligation, classify_channel


def _ob(line: str, *, operation: str = "added", exclusive: bool = False,
        status: str = "MISSING") -> BranchObligation:
    return BranchObligation(
        line=line, channel=classify_channel(line), status=status,
        side="replayed", operation=operation, exclusive=exclusive,
    )


class _Hostile:
    """An obligation whose attribute access raises — the engine must
    catch and return BLOCKED with the original text, never propagate."""

    def __getattr__(self, name):  # pragma: no cover - raises by design
        raise RuntimeError(f"hostile attribute: {name}")


def _field(text, obs):
    from capybase.named_field_union import propose_named_field_union
    return propose_named_field_union(
        text, obs,
        other_side_text=(
            "struct State<S> {\n"
            "    stream: S,\n"
            "    _marker: PhantomData<fn() -> S>,\n"
            "}\n"),
    )


def _item(text, obs):
    from capybase.keyed_item_union import propose_keyed_item_union
    return propose_keyed_item_union(
        text, obs,
        other_side_text=(
            "impl Client {\n"
            "    fn encode(&self) {}\n"
            "    fn decode(&mut self) {\n"
            "        self.buf.clear();\n"
            "    }\n"
            "}\n"),
    )


def _attribute(text, obs):
    from capybase.attribute_meta_union import propose_attribute_meta_union
    return propose_attribute_meta_union(text, obs)


def _import(text, obs):
    from capybase.import_union import propose_import_union
    return propose_import_union(text, obs)


def _manifest(text, obs):
    from capybase.manifest_union import propose_manifest_union
    return propose_manifest_union(text, obs)


#: name → (call, resolved_text, one APPLICABLE obligation line,
#:          expected mechanism id)
PRIMITIVES = {
    "manifest": (
        _manifest,
        'tokio = { version = "1.0", features = ["rt"] }\n',
        'tokio = { version = "1.0", features = ["macros"] }',
        "toml.manifest_union/v1"),
    "named_field": (
        _field,
        "struct State<S> {\n    stream: S,\n}\n",
        "    _marker: PhantomData<fn() -> S>,",
        "rust.named_field_union/v1"),
    "keyed_item": (
        _item,
        "impl Client {\n    fn encode(&self) {}\n}\n",
        "    fn decode(&mut self) {",
        "rust.keyed_item_union/v1"),
    "attribute": (
        _attribute,
        "#[derive(Debug)]\nstruct S { x: u32 }",
        "#[derive(Clone)]",
        "rust.attribute_meta_union/v1"),
    "import": (
        _import,
        "use std::collections::{HashMap, BTreeMap};\nfn main(){}",
        "use std::collections::HashSet;",
        "rust.use_leaf_union/v1"),
}
IDS = list(PRIMITIVES)


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


class TestUniversalCodecContract:

    @pytest.mark.parametrize("pid", IDS)
    def test_applicable_case_actually_applies(self, pid):
        """Fixture honesty: the table's case must reach APPLIED."""
        call, text, line, _ = PRIMITIVES[pid]
        r = call(text, [_ob(line)])
        assert str(r.status) == "APPLIED", (
            f"{pid}: fixture must apply (got {r.status}: "
            f"{r.certificate.get('reason', '')})")

    @pytest.mark.parametrize("pid", IDS)
    def test_never_raises_on_garbage(self, pid):
        call = PRIMITIVES[pid][0]
        for text in ("", "\x00\x01\xff", "ünïcödé 🦀 text", "{[((<",
                     "a" * 100_000):
            r = call(text, [_ob("fn real_line() {}")])
            assert str(r.status) in (
                "APPLIED", "NOT_APPLICABLE", "BLOCKED", "AMBIGUOUS")
            assert isinstance(r.text, str)
        # No obligations at all / None.
        assert str(call("fn main(){}", None).status) == "NOT_APPLICABLE"
        assert str(call("fn main(){}", []).status) == "NOT_APPLICABLE"

    @pytest.mark.parametrize("pid", IDS)
    def test_hostile_obligation_object_blocks_cleanly(self, pid):
        """An obligation whose attribute access raises must surface as
        BLOCKED with the ORIGINAL text (transactional rollback), never
        propagate the exception."""
        call, text, line, _ = PRIMITIVES[pid]
        r = call(text, [_Hostile()])
        assert str(r.status) == "BLOCKED"
        assert r.text == text

    @pytest.mark.parametrize("pid", IDS)
    def test_deterministic(self, pid):
        call, text, line, _ = PRIMITIVES[pid]
        r1 = call(text, [_ob(line), _ob("# comment-only", operation="added")])
        r2 = call(text, [_ob(line), _ob("# comment-only", operation="added")])
        assert (str(r1.status), r1.text, r1.certificate) == \
               (str(r2.status), r2.text, r2.certificate)

    @pytest.mark.parametrize("pid", IDS)
    def test_idempotent_reentry(self, pid):
        """Applying to the already-edited text is a no-op: the text is
        stable (no double application)."""
        call, text, line, _ = PRIMITIVES[pid]
        r1 = call(text, [_ob(line)])
        assert str(r1.status) == "APPLIED"
        r2 = call(r1.text, [_ob(line)])
        assert str(r2.status) == "NOT_APPLICABLE", (
            f"{pid}: re-entry must not re-apply (got {r2.status})")
        assert r2.text == r1.text

    @pytest.mark.parametrize("pid", IDS)
    def test_filter_contract(self, pid):
        """Only added + non-exclusive obligations are acted on;
        everything else is a clean NOT_APPLICABLE no-op."""
        call, text, line, _ = PRIMITIVES[pid]
        inert = [
            _ob(line, operation="removed"),
            _ob(line, operation="modified"),
            _ob(line, exclusive=True),
        ]
        r = call(text, inert)
        assert str(r.status) == "NOT_APPLICABLE"
        assert r.text == text

    # Documented deviation: the import primitive's ORIGINAL filter (kept
    # byte-for-byte at the switch) never checked status == "MISSING" —
    # only operation + exclusive. Production obligations always carry
    # MISSING status (derive_missing_obligations), so the asymmetry is
    # unobservable live. Harmonizing (adding the status check) is a
    # deliberate behavior change for AFTER the harvest, not a silent
    # side effect of a refactor.
    STATUS_CHECKING = [p for p in IDS if p != "import"]

    @pytest.mark.parametrize("pid", STATUS_CHECKING)
    def test_present_status_is_inert(self, pid):
        call, text, line, _ = PRIMITIVES[pid]
        r = call(text, [_ob(line, status="PRESENT")])
        assert str(r.status) == "NOT_APPLICABLE"
        assert r.text == text

    @pytest.mark.parametrize("pid", IDS)
    def test_applied_certificate_contract(self, pid):
        call, text, line, mechanism = PRIMITIVES[pid]
        r = call(text, [_ob(line)])
        cert = r.certificate
        assert cert["primitive"] == mechanism
        assert cert["risk_tier"] == "A"
        assert cert["before_hash"] == _hash(text)
        assert cert["after_hash"] == _hash(r.text)
        assert cert["before_hash"] != cert["after_hash"]
        assert cert.get("closed_obligations"), "must close the obligation"

    @pytest.mark.parametrize("pid", IDS)
    def test_non_applied_is_byte_identical(self, pid):
        """Transactional honesty: whenever the status is not APPLIED,
        the returned text is the input byte-for-byte."""
        call, text, line, _ = PRIMITIVES[pid]
        cases = [
            [],                                # nothing to do
            [_ob("# a comment line")],          # filtered by channel
            [_ob("not an obligation shape ~~~")],
            [_ob(line, exclusive=True)],        # choice → the model
        ]
        for obs in cases:
            r = call(text, obs)
            assert str(r.status) == "NOT_APPLICABLE"
            assert r.text == text, f"{pid}: mutated text on {obs!r}"
