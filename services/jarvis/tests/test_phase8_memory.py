"""Memory: what Jarvis learns, when it acts on it, and how it forgets.

The tests that matter most here are the ones about *not* learning. A memory
system that records too eagerly is worse than none — a wrong belief persists
and quietly shapes every later turn, and the user has no idea why Jarvis
started behaving oddly.
"""

from __future__ import annotations

import pytest

from jarvis.agents.types import EventType
from jarvis.db.engine import Database
from jarvis.memory import learning
from jarvis.memory.embeddings import (
    LexicalEmbedder,
    MiniLMEmbedder,
    best_available,
    cosine,
    pack,
    tokenise,
    unpack,
)
from jarvis.memory.store import (
    MAX_CONFIDENCE,
    PROMOTION_THRESHOLD,
    MemoryContext,
    MemoryStore,
)


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore(Database(":memory:"))


# ── embeddings ───────────────────────────────────────────────────────────
class TestEmbeddings:
    def test_related_text_scores_higher_than_unrelated(self) -> None:
        e = LexicalEmbedder()
        related = cosine(e.embed("open pdfs in acrobat"), e.embed("acrobat opens my pdf files"))
        unrelated = cosine(e.embed("open pdfs in acrobat"), e.embed("the quietest keyboard"))
        assert related > unrelated
        assert related > 0.2

    def test_identical_text_is_maximally_similar(self) -> None:
        e = LexicalEmbedder()
        assert cosine(e.embed("dark mode"), e.embed("dark mode")) == pytest.approx(1.0)

    def test_buckets_survive_a_restart(self) -> None:
        """Regression guard: Python's hash() is salted per process.

        Using it would put the same word in a different bucket after every
        restart, so every stored vector would silently stop matching.
        """
        assert LexicalEmbedder().embed("acrobat") == LexicalEmbedder().embed("acrobat")

    def test_stopwords_carry_no_signal(self) -> None:
        assert tokenise("the and of is it") == []
        assert (
            cosine(LexicalEmbedder().embed("the and of"), LexicalEmbedder().embed("acrobat")) == 0
        )

    def test_vectors_round_trip_through_storage(self) -> None:
        vector = LexicalEmbedder().embed("save screenshots to pictures")
        assert unpack(pack(vector)) == pytest.approx(vector, abs=1e-6)

    def test_mismatched_dimensions_score_zero_rather_than_crashing(self) -> None:
        assert cosine([1.0, 0.0], [1.0, 0.0, 0.0]) == 0.0

    def test_the_semantic_model_says_what_is_missing(self) -> None:
        available, detail = MiniLMEmbedder().available()
        if not available:
            assert "MiniLM" in detail or "onnxruntime" in detail
            assert "90 MB" in detail or "not been downloaded" in detail

    def test_a_missing_model_never_returns_a_stub_vector(self) -> None:
        """Zeros would make every memory equally similar to every query.

        That reads as bad recall rather than a missing model, which is exactly
        the kind of silent degradation this project refuses.
        """
        model = MiniLMEmbedder()
        if not model.available()[0]:
            with pytest.raises((RuntimeError, NotImplementedError)):
                model.embed("anything")

    def test_the_fallback_is_a_real_embedder_not_a_stub(self) -> None:
        chosen = best_available()
        assert chosen.available()[0]
        assert any(v != 0.0 for v in chosen.embed("open pdfs in acrobat"))


# ── promotion ────────────────────────────────────────────────────────────
class TestPromotion:
    def test_one_observation_is_not_a_belief(self, store: MemoryStore) -> None:
        result = store.remember("semantic", "app.open.pdf", "Acrobat")
        assert result.memory.status == "candidate"
        assert not result.promoted

    def test_three_consistent_observations_promote(self, store: MemoryStore) -> None:
        for _ in range(PROMOTION_THRESHOLD - 1):
            assert not store.remember("semantic", "app.open.pdf", "Acrobat").memory.active
        result = store.remember("semantic", "app.open.pdf", "Acrobat")
        assert result.memory.active
        assert result.promoted

    def test_a_stated_preference_is_acted_on_at_once(self, store: MemoryStore) -> None:
        result = store.remember("semantic", "preference.theme", "dark", source="stated")
        assert result.memory.active
        assert result.memory.confidence >= 0.9

    def test_a_contradiction_restarts_the_count(self, store: MemoryStore) -> None:
        """ "I no longer know" is the honest conclusion, not a confident average."""
        for _ in range(PROMOTION_THRESHOLD):
            store.remember("semantic", "app.open.pdf", "Acrobat")
        result = store.remember("semantic", "app.open.pdf", "Chrome")
        assert result.contradicted
        assert result.memory.observation_count == 1
        assert result.memory.status == "candidate"
        assert result.memory.value == "Chrome"

    def test_a_statement_overrides_observations_immediately(self, store: MemoryStore) -> None:
        store.remember("semantic", "app.open.pdf", "Acrobat")
        result = store.remember("semantic", "app.open.pdf", "Chrome", source="stated")
        assert result.memory.active
        assert result.memory.value == "Chrome"

    def test_confidence_never_reaches_certainty(self, store: MemoryStore) -> None:
        for _ in range(40):
            store.remember("semantic", "app.open.pdf", "Acrobat")
        memory = store.get("semantic", "app.open.pdf")
        assert memory is not None
        assert memory.confidence <= MAX_CONFIDENCE < 1.0

    def test_one_row_per_key_rather_than_a_log(self, store: MemoryStore) -> None:
        for _ in range(5):
            store.remember("semantic", "app.open.pdf", "Acrobat")
        assert store.count() == 1

    def test_an_unknown_tier_is_refused(self, store: MemoryStore) -> None:
        with pytest.raises(ValueError, match="unknown tier"):
            store.remember("telepathic", "k", "v")

    def test_explanations_are_written_for_a_person(self, store: MemoryStore) -> None:
        assert "won't act on it yet" in store.remember("semantic", "k", "v").explain()
        assert "Seen 2 times" in store.remember("semantic", "k", "v").explain()
        assert "I'll remember that" in store.remember("semantic", "k", "v").explain()


# ── retrieval ────────────────────────────────────────────────────────────
class TestRetrieval:
    def test_a_stated_preference_is_recalled(self, store: MemoryStore) -> None:
        store.remember("semantic", "app.open.pdf", "Acrobat", source="stated")
        hits = store.recall_for("what should I open a pdf with")
        assert [m.key for m in hits] == ["app.open.pdf"]

    def test_candidates_are_never_recalled(self, store: MemoryStore) -> None:
        """Something seen once must not shape an answer."""
        store.remember("semantic", "app.open.pdf", "Acrobat")
        assert store.recall_for("open a pdf") == []

    def test_unrelated_memories_are_not_recalled(self, store: MemoryStore) -> None:
        store.remember("semantic", "app.open.pdf", "Acrobat", source="stated")
        assert [m.key for m in store.recall_for("what is the capital of France")] == []

    def test_recall_counts_usage(self, store: MemoryStore) -> None:
        store.remember("semantic", "app.open.pdf", "Acrobat", source="stated")
        store.recall_for("open a pdf")
        memory = store.get("semantic", "app.open.pdf")
        assert memory is not None
        assert memory.use_count == 1
        assert memory.last_used_at

    def test_pinned_memories_outrank_equals(self, store: MemoryStore) -> None:
        store.remember("semantic", "note.coffee", "I drink coffee", source="stated")
        second = store.remember("semantic", "note.tea", "I drink tea", source="stated")
        store.pin(second.memory.id)
        hits = store.search("drink")
        assert hits[0].key == "note.tea"

    def test_vectors_from_another_embedder_are_ignored(self, store: MemoryStore) -> None:
        """A cosine between two models' vectors is a confident, meaningless number."""
        store.remember("semantic", "app.open.pdf", "Acrobat", source="stated")
        store.db.execute("UPDATE memory_vec SET embedder = 'some-other-model'")
        hits = store.search("app.open.pdf")
        # The exact key match still works; the similarity term contributes nothing.
        assert [m.key for m in hits] == ["app.open.pdf"]
        assert hits[0].score < 1.0

    def test_reindexing_restores_search_after_a_model_change(self, store: MemoryStore) -> None:
        store.remember("semantic", "note.keyboards", "I like quiet keyboards", source="stated")
        store.db.execute("UPDATE memory_vec SET embedder = 'some-other-model'")
        assert store.search("quiet keyboards") == []
        assert store.reindex() == 1
        assert [m.key for m in store.search("quiet keyboards")] == ["note.keyboards"]


# ── forgetting ───────────────────────────────────────────────────────────
class TestForgetting:
    def test_forget_deletes_rather_than_hides(self, store: MemoryStore) -> None:
        result = store.remember("semantic", "preference.theme", "dark", source="stated")
        assert store.forget(result.memory.id)
        assert store.count() == 0
        row = store.db.query_one("SELECT COUNT(*) AS n FROM memory_vec")
        assert row is not None
        assert row["n"] == 0, "the vector must go with the memory"

    def test_forgetting_by_phrase_reports_what_went(self, store: MemoryStore) -> None:
        store.remember("semantic", "preference.theme", "dark", source="stated")
        gone = store.forget_matching("forget that I prefer dark mode")
        assert [m.key for m in gone] == ["preference.theme"]
        assert store.count() == 0

    def test_forgetting_a_phrase_that_matches_nothing_removes_nothing(
        self, store: MemoryStore
    ) -> None:
        store.remember("semantic", "preference.theme", "dark", source="stated")
        assert store.forget_matching("forget my cat's birthday") == []
        assert store.count() == 1

    def test_clearing_one_tier_leaves_the_others(self, store: MemoryStore) -> None:
        store.remember("semantic", "a", "1", source="stated")
        store.remember("episodic", "b", "2", source="stated")
        assert store.clear("episodic") == 1
        assert store.count() == 1


# ── privacy ──────────────────────────────────────────────────────────────
class TestPrivacy:
    def test_secrets_are_stripped_before_storage(self, store: MemoryStore) -> None:
        """Memory is long-lived and derived from free text — the worst place for a key."""
        result = store.remember(
            "semantic",
            "note.key",
            "my token is sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGGHHHHIIIIJJJJ",
            source="stated",
        )
        assert "sk-ant" not in str(result.memory.value)
        assert "[redacted]" in str(result.memory.value)

    def test_an_edit_counts_as_a_statement(self, store: MemoryStore) -> None:
        created = store.remember("semantic", "app.open.pdf", "Acrobat")
        assert created.memory.status == "candidate"
        edited = store.set_value(created.memory.id, "Chrome")
        assert edited is not None
        assert edited.value == "Chrome"
        assert edited.active, "correcting a memory by hand must take effect at once"

    def test_provenance_is_recorded(self, store: MemoryStore) -> None:
        result = store.remember(
            "semantic", "preference.theme", "dark", source="stated", session_id="sess_1"
        )
        assert result.memory.session_id == "sess_1"
        assert result.memory.source == "stated"
        assert result.memory.created_at

    def test_stats_name_the_embedder_honestly(self, store: MemoryStore) -> None:
        stats = store.stats()
        assert stats["embedder"] == "lexical-v1"
        assert stats["semantic"] is False, "word matching must not be reported as semantic"
        assert "word" in str(stats["embedderDetail"]).lower()


# ── the prompt block ─────────────────────────────────────────────────────
class TestPromptBlock:
    def test_memories_are_labelled_as_beliefs(self, store: MemoryStore) -> None:
        store.remember("semantic", "preference.theme", "dark", source="stated")
        block = MemoryContext(store.recall_for("theme")).prompt_block()
        assert "beliefs, not" in block
        assert "the user is right" in block
        assert "preference.theme: dark" in block

    def test_an_empty_context_adds_nothing_to_the_prompt(self) -> None:
        assert MemoryContext().prompt_block() == ""

    def test_confidence_is_shown_to_the_model(self, store: MemoryStore) -> None:
        store.remember("semantic", "preference.theme", "dark", source="stated")
        assert "90%" in MemoryContext(store.recall_for("theme")).prompt_block()


# ── what gets learned from a message ─────────────────────────────────────
class TestLearning:
    @pytest.mark.parametrize(
        ("message", "key", "value"),
        [
            ("always open pdfs in Acrobat", "app.open.pdf", "Acrobat"),
            ("from now on open docx files with LibreOffice", "app.open.docx", "LibreOffice"),
            ("call me Fawad", "user.name", "Fawad"),
            ("I prefer dark mode", "preference.theme", "dark"),
            ("I prefer brief answers", "preference.style", "brief"),
            ("always save screenshots to Pictures", "folder.screenshots", "Pictures"),
        ],
    )
    def test_statements_are_extracted(self, message: str, key: str, value: str) -> None:
        statements = learning.extract(message)
        assert statements, f"nothing extracted from {message!r}"
        assert (statements[0].key, statements[0].value) == (key, value)

    @pytest.mark.parametrize(
        "message",
        [
            "open the pdf in acrobat",  # a one-off request, not a preference
            "what is the weather",
            "list my downloads",
            "can you open chrome",
            "",
        ],
    )
    def test_ordinary_requests_teach_nothing(self, message: str) -> None:
        """The most important test here: eager learning is worse than none."""
        assert learning.extract(message) == []

    def test_a_free_form_statement_becomes_an_editable_note(self) -> None:
        statements = learning.extract("remember that I work night shifts")
        assert statements[0].key == "note.work-night-shifts"
        assert statements[0].value == "I work night shifts"

    def test_the_same_statement_twice_produces_the_same_key(self) -> None:
        """Otherwise a repeat creates a duplicate instead of corroborating."""
        first = learning.extract("remember that I work night shifts")[0]
        second = learning.extract("always remember that I work night shifts")[0]
        assert first.key == second.key

    def test_forgetting_is_recognised_and_never_learned_from(self) -> None:
        assert learning.is_forget("forget that I prefer dark mode")
        assert learning.extract("forget that I prefer dark mode") == []

    def test_a_failed_action_teaches_nothing(self) -> None:
        """A failed launch would teach a preference for a missing application."""
        assert (
            learning.from_tool_use("application", {"operation": "launch", "name": "x"}, False)
            is None
        )

    def test_a_successful_launch_is_observed(self) -> None:
        statement = learning.from_tool_use(
            "application", {"operation": "launch", "name": "Spotify"}, True
        )
        assert statement is not None
        assert statement.value == "Spotify"

    def test_an_observation_from_a_tool_is_not_immediately_believed(
        self, store: MemoryStore
    ) -> None:
        statement = learning.from_tool_use(
            "application", {"operation": "launch", "name": "Spotify"}, True
        )
        assert statement is not None
        result = store.remember(statement.tier, statement.key, statement.value)
        assert result.memory.status == "candidate"


# ── the turn loop ────────────────────────────────────────────────────────
class TestTurnLoop:
    async def test_a_preference_stated_once_is_recalled_in_a_later_session(self, ctx) -> None:
        """The Phase 8 gate, stated as a test."""
        first = ctx.sessions.create("first")
        learned = [
            e
            async for e in ctx.orchestrator.handle(first.id, "always open pdfs in Acrobat")
            if e.type is EventType.MEMORY_LEARNED
        ]
        assert learned, "nothing was learned from an explicit statement"
        assert "Acrobat" in learned[0].data["message"]

        # A different session entirely: memory that only worked within one
        # conversation would be a transcript, not a memory.
        second = ctx.sessions.create("second")
        recalled = [
            e
            async for e in ctx.orchestrator.handle(second.id, "what do I open pdfs with?")
            if e.type is EventType.MEMORY_RECALLED
        ]
        assert recalled, "the preference was not recalled in a later session"
        assert recalled[0].data["memories"][0]["key"] == "app.open.pdf"

    async def test_forgetting_is_reported_with_what_went(self, ctx) -> None:
        session = ctx.sessions.create("s")
        async for _ in ctx.orchestrator.handle(session.id, "I prefer dark mode"):
            pass
        assert ctx.memory.count() == 1
        events = [
            e
            async for e in ctx.orchestrator.handle(session.id, "forget that I prefer dark mode")
            if e.type is EventType.MEMORY_FORGOTTEN
        ]
        assert events
        assert "preference.theme" in events[0].data["message"]
        assert ctx.memory.count() == 0

    async def test_an_unrecognised_memory_request_says_so(self, ctx) -> None:
        """A half-finished instruction must not become a half-understood belief."""
        session = ctx.sessions.create("s")
        notices = [
            e
            async for e in ctx.orchestrator.handle(session.id, "from now on")
            if e.type is EventType.NOTICE
        ]
        assert notices
        assert "couldn't work out what to remember" in notices[0].data["message"]
        assert ctx.memory.count() == 0

    async def test_a_turn_always_ends(self, ctx) -> None:
        """Whatever happens, the interface must stop waiting."""
        session = ctx.sessions.create("s")
        for message in ("call me Fawad", "forget my name", "from now on", "remember my cat"):
            kinds = [e.type async for e in ctx.orchestrator.handle(session.id, message)]
            assert EventType.TURN_END in kinds, f"{message!r} left the turn open"

    async def test_an_ordinary_chat_message_learns_nothing(self, ctx) -> None:
        session = ctx.sessions.create("s")
        async for _ in ctx.orchestrator.handle(session.id, "what is the capital of France"):
            pass
        assert ctx.memory.count() == 0


# ── the dashboard API ────────────────────────────────────────────────────
class TestMemoryApi:
    async def test_listing_includes_candidates_and_stats(self, client, ctx) -> None:
        ctx.memory.remember("semantic", "preference.theme", "dark", source="stated")
        ctx.memory.remember("semantic", "app.open.pdf", "Acrobat")  # a candidate

        response = await client.get("/memory")
        assert response.status_code == 200
        body = response.json()
        keys = {m["key"] for m in body["memories"]}
        assert keys == {"preference.theme", "app.open.pdf"}
        assert body["stats"]["total"] == 2
        assert body["stats"]["embedder"]

    async def test_a_candidate_is_visibly_a_candidate(self, client, ctx) -> None:
        """Seeing what Jarvis is about to believe, before it acts, is the point."""
        ctx.memory.remember("semantic", "app.open.pdf", "Acrobat")
        body = (await client.get("/memory")).json()
        assert body["memories"][0]["status"] == "candidate"
        assert body["memories"][0]["source"] == "observed"

    async def test_searching_filters(self, client, ctx) -> None:
        ctx.memory.remember("semantic", "preference.theme", "dark", source="stated")
        ctx.memory.remember("semantic", "note.keyboards", "quiet keyboards", source="stated")
        body = (await client.get("/memory?q=keyboards")).json()
        assert [m["key"] for m in body["memories"]] == ["note.keyboards"]

    async def test_editing_a_memory_makes_it_active(self, client, ctx) -> None:
        created = ctx.memory.remember("semantic", "app.open.pdf", "Acrobat")
        response = await client.patch(f"/memory/{created.memory.id}", json={"value": "Chrome"})
        assert response.status_code == 200
        assert response.json()["value"] == "Chrome"
        assert response.json()["status"] == "active"

    async def test_pinning(self, client, ctx) -> None:
        created = ctx.memory.remember("semantic", "a", "1", source="stated")
        response = await client.patch(f"/memory/{created.memory.id}", json={"pinned": True})
        assert response.json()["pinned"] is True

    async def test_editing_something_that_does_not_exist_is_404(self, client) -> None:
        assert (await client.patch("/memory/mem_nope", json={"value": "x"})).status_code == 404

    async def test_an_empty_patch_is_rejected_rather_than_ignored(self, client, ctx) -> None:
        created = ctx.memory.remember("semantic", "a", "1", source="stated")
        assert (await client.patch(f"/memory/{created.memory.id}", json={})).status_code == 422

    async def test_deleting_removes_it_for_real(self, client, ctx) -> None:
        created = ctx.memory.remember("semantic", "a", "1", source="stated")
        assert (await client.delete(f"/memory/{created.memory.id}")).status_code == 200
        assert ctx.memory.count() == 0
        assert (await client.delete(f"/memory/{created.memory.id}")).status_code == 404

    async def test_clearing_everything(self, client, ctx) -> None:
        for key in ("a", "b", "c"):
            ctx.memory.remember("semantic", key, "1", source="stated")
        body = (await client.delete("/memory")).json()
        assert body["deleted"] == 3
        assert ctx.memory.count() == 0

    async def test_memory_endpoints_need_a_token(self, app) -> None:
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as anon:
            assert (await anon.get("/memory")).status_code == 401
            assert (await anon.delete("/memory")).status_code == 401

    async def test_edits_and_deletions_are_audited(self, client, ctx) -> None:
        """A memory change is a change to how Jarvis behaves, so it is recorded."""
        created = ctx.memory.remember("semantic", "a", "1", source="stated")
        await client.patch(f"/memory/{created.memory.id}", json={"value": "2"})
        await client.delete(f"/memory/{created.memory.id}")
        actions = {e["action"] for e in ctx.audit.recent(limit=20)}
        assert {"memory.edit", "memory.forget"} <= actions
        assert ctx.audit.verify()[0], "the audit chain must still be intact"
