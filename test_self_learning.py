import math
import tempfile
import unittest
from pathlib import Path

from agent import LocalAgent
from self_learning import SelfLearner
from semantic_memory import SemanticMemory
from workspace_tools import WorkspaceTools


def _vector(text):
    """متجه حتمي بسيط للاختبار: يعكس تشابه النصوص المشتركة في الكلمات."""
    words = set(text.split())
    dims = ["ملف", "بايثون", "مشروع", "اختبار", "تفضيل", "درس"]
    base = [1.0 if word in " ".join(words) else 0.0 for word in dims]
    if not any(base):
        base = [0.1] * len(dims)
    norm = math.sqrt(sum(v * v for v in base)) or 1.0
    return [v / norm for v in base]


class FakeClient:
    """عميل وهمي: chat يعيد ردودًا مبرمجة، embed حتمي."""

    def __init__(self, chat_replies):
        self.chat_replies = list(chat_replies)
        self.chat_calls = []

    def chat(self, messages, tools, model):
        self.chat_calls.append(messages)
        content = self.chat_replies.pop(0)
        return {"choices": [{"message": {"role": "assistant", "content": content}}]}

    def embed(self, texts, model):
        return [_vector(text) for text in texts]


class SelfLearnerTests(unittest.TestCase):
    def _memory(self, temp_dir):
        return SemanticMemory(Path(temp_dir) / "learned.json")

    def test_extracts_and_stores_lessons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient(['["المستخدم يفضّل بايثون في هذا المشروع"]'])
            learner = SelfLearner(memory)
            saved = learner.learn(
                "ما لغة المشروع؟", "المشروع بايثون", client, "gemma", "embed"
            )
            self.assertEqual(saved, 1)
            records = memory.recall("تفضيل بايثون", client, "embed", 5)
            self.assertTrue(any("بايثون" in r["text"] for r in records))

    def test_empty_array_stores_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient(["[]"])
            learner = SelfLearner(memory)
            self.assertEqual(
                learner.learn("مرحبا", "أهلا", client, "gemma", "embed"), 0
            )

    def test_ignores_non_string_and_too_short_items(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient(['["قصير", 42, "درس دائم صالح للحفظ هنا"]'])
            learner = SelfLearner(memory)
            self.assertEqual(
                learner.learn("س", "ج طويل بما يكفي", client, "gemma", "embed"), 1
            )

    def test_malformed_json_is_safe(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient(["ليس JSON على الإطلاق"])
            learner = SelfLearner(memory)
            self.assertEqual(learner.learn("س", "ج", client, "gemma", "embed"), 0)

    def test_recall_note_filters_by_score(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient([])
            memory.remember(["المستخدم يفضّل بايثون في المشروع"], client, "embed")
            learner = SelfLearner(memory, min_score=0.3)
            note = learner.recall_note("سؤال عن بايثون المشروع", client, "embed")
            self.assertIsNotNone(note)
            self.assertIn("بايثون", note)
            self.assertIn("غير موثوقة", note)

    def test_recall_note_none_when_empty(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = self._memory(temp_dir)
            client = FakeClient([])
            learner = SelfLearner(memory)
            self.assertIsNone(learner.recall_note("أي شيء", client, "embed"))

    def test_agent_learns_after_answer_and_recalls_next_turn(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            memory = SemanticMemory(Path(temp_dir) / "learned.json")
            learner = SelfLearner(memory, min_score=0.2)
            # الرد الأول ثم استخلاص درس، ثم الرد الثاني.
            client = FakeClient(
                [
                    "المشروع مكتوب بلغة بايثون",
                    '["المستخدم يعمل على مشروع بايثون"]',
                    "نعم، بايثون",
                ]
            )
            agent = LocalAgent(
                client,
                WorkspaceTools(temp_dir),
                model="gemma",
                embed_model="embed",
                learner=learner,
            )
            first = agent.answer("ما لغة المشروع؟")
            self.assertEqual(first, "المشروع مكتوب بلغة بايثون")
            # الدرس حُفظ.
            self.assertTrue(memory.recall("مشروع بايثون", client, "embed", 5))
            # الطلب الثاني يجب أن يحقن الدرس كسياق مستخدم غير موثوق، لا كرسالة نظام.
            agent.answer("سؤال بايثون آخر")
            # نداء الرد يحمل رسالة المستخدم نصًا صريحًا، بخلاف نداء الاستخلاص
            # الذي يضع النص داخل قالب «سؤال المستخدم:».
            answer_calls = [
                messages
                for messages in client.chat_calls
                if any(
                    m.get("role") == "user" and m.get("content") == "سؤال بايثون آخر"
                    for m in messages
                )
            ]
            self.assertTrue(answer_calls)
            self.assertTrue(
                any(
                    m.get("role") == "user" and "دروس مكتسبة" in m.get("content", "")
                    for m in answer_calls[-1]
                )
            )


if __name__ == "__main__":
    unittest.main()
