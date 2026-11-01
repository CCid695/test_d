"""Unit tests for mini_devin.Agent.run()'s tool-calling loop: the
min_tool_calls "keep going" mechanism and the max_seconds wall-clock cap —
both added after real testing showed the integrator fabricating an
elaborate verification narrative, and separately getting "stuck" for
minutes with no time bound. Uses a fake LLM client and a fake sandbox — no
Ollama, no Docker/K8s needed.
"""

from types import SimpleNamespace

import mini_devin as md


class FakeToolCall:
    def __init__(self, call_id, name, arguments):
        self.id = call_id
        self.function = SimpleNamespace(name=name, arguments=arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or None

    def model_dump(self, exclude_none=True):
        return {"role": "assistant", "content": self.content}


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0
        self.received_kwargs = []

    def create(self, model, messages, tools, **kwargs):
        self.calls += 1
        self.received_kwargs.append(kwargs)
        if not self._responses:
            raise AssertionError("FakeLLM ran out of canned responses")
        return SimpleNamespace(choices=[SimpleNamespace(message=self._responses.pop(0))])


class FakeLLM:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)
        self.chat = SimpleNamespace(completions=self.completions)


class FakeSandbox:
    def __init__(self):
        self.commands = []

    def start(self, log, should_stop):
        return "http://fake/"

    def exec(self, command, stdin_bytes=None):
        self.commands.append(command)
        return 0, "ok"

    def put_archive(self, tar_bytes):
        pass

    def remove(self):
        pass


def _make_agent(llm):
    agent = md.Agent("x", sandbox=FakeSandbox(), llm=llm, log=lambda *_: None)
    agent.start_sandbox()
    return agent


def test_default_behavior_unaffected_when_min_tool_calls_is_none():
    llm = FakeLLM([FakeMessage(content="done, no tools needed")])
    agent = _make_agent(llm)

    result = agent.run("do something", max_steps=5)

    assert result == "done, no tools needed"
    assert llm.completions.calls == 1


def test_min_tool_calls_forces_continuation_when_model_tries_to_stop_early():
    responses = [
        FakeMessage(tool_calls=[FakeToolCall("1", "run_command", '{"command": "ls"}')]),  # 1 real call
        FakeMessage(content="all done!"),  # tries to stop with only 1 call -> must be nudged, not accepted
        FakeMessage(tool_calls=[FakeToolCall("2", "run_command", '{"command": "cat x"}')]),  # 2nd real call
        FakeMessage(content="now really done"),  # stops with 2 calls -> accepted
    ]
    llm = FakeLLM(responses)
    agent = _make_agent(llm)

    result = agent.run("verify something", max_steps=10, min_tool_calls=2)

    assert result == "now really done"
    assert agent.sandbox.commands == ["ls", "cat x"]
    assert llm.completions.calls == 4  # the premature-stop attempt still consumed one real LLM call


def test_min_tool_calls_hits_max_steps_if_model_never_calls_enough_tools():
    responses = [FakeMessage(content="nothing to do") for _ in range(5)]
    llm = FakeLLM(responses)
    agent = _make_agent(llm)

    result = agent.run("verify something", max_steps=5, min_tool_calls=3)

    assert result == "(reached max steps without a final answer)"
    assert llm.completions.calls == 5


def test_custom_nudge_text_is_sent_back_to_the_model():
    seen_messages = []

    class RecordingCompletions(FakeCompletions):
        def create(self, model, messages, tools):
            seen_messages.append(list(messages))
            return super().create(model, messages, tools)

    responses = [
        FakeMessage(content="stop1"),  # 0 real calls < 1 -> must be nudged, not accepted
        FakeMessage(tool_calls=[FakeToolCall("1", "run_command", '{"command": "ls"}')]),  # 1 real call now
        FakeMessage(content="stop2 for real"),  # 1 real call >= 1 -> accepted
    ]
    completions = RecordingCompletions(responses)
    llm = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = _make_agent(llm)

    agent.run("verify something", max_steps=5, min_tool_calls=1, nudge="MY CUSTOM NUDGE")

    # The second call's message history must include our nudge text, appended
    # after the model's first (rejected) attempt to stop.
    second_call_messages = seen_messages[1]
    assert any(m.get("content") == "MY CUSTOM NUDGE" for m in second_call_messages)


def test_max_seconds_none_does_not_pass_a_timeout_kwarg():
    llm = FakeLLM([FakeMessage(content="done")])
    agent = _make_agent(llm)

    agent.run("do something", max_steps=5)

    assert "timeout" not in llm.completions.received_kwargs[0]


def test_max_seconds_passes_remaining_budget_as_call_timeout():
    responses = [
        FakeMessage(tool_calls=[FakeToolCall("1", "run_command", '{"command": "ls"}')]),
        FakeMessage(content="done"),
    ]
    llm = FakeLLM(responses)
    agent = _make_agent(llm)

    agent.run("do something", max_steps=5, max_seconds=100)

    first_timeout = llm.completions.received_kwargs[0]["timeout"]
    second_timeout = llm.completions.received_kwargs[1]["timeout"]
    assert 0 < first_timeout <= 100
    # The second call's budget must not exceed the first: real time elapsed since start.
    assert second_timeout <= first_timeout


def test_max_seconds_stops_without_calling_llm_once_budget_is_exhausted():
    class ClockAdvancingCompletions(FakeCompletions):
        """Each call reports being instantaneous to FakeLLM's bookkeeping,
        but manually advances a fake clock so the budget runs out after
        exactly 2 calls without needing a real sleep."""

        def __init__(self, responses, advance_fn):
            super().__init__(responses)
            self._advance = advance_fn

        def create(self, model, messages, tools, **kwargs):
            self._advance()
            return super().create(model, messages, tools, **kwargs)

    fake_now = [0.0]

    def fake_monotonic():
        return fake_now[0]

    def advance():
        fake_now[0] += 0.5

    responses = [
        FakeMessage(tool_calls=[FakeToolCall("1", "run_command", '{"command": "ls"}')]),
        FakeMessage(tool_calls=[FakeToolCall("2", "run_command", '{"command": "ls"}')]),
        FakeMessage(content="should never get here"),
    ]
    completions = ClockAdvancingCompletions(responses, advance)
    llm = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = _make_agent(llm)

    original_monotonic = md.time.monotonic
    md.time.monotonic = fake_monotonic
    try:
        result = agent.run("verify something", max_steps=10, max_seconds=1.0)
    finally:
        md.time.monotonic = original_monotonic

    assert "超过" in result
    assert "2 次真实工具调用" in result
    # The 3rd LLM call must never happen once the budget is gone.
    assert completions.calls == 2


def test_max_seconds_treats_a_call_exception_near_the_deadline_as_timeout():
    class TimingOutCompletions(FakeCompletions):
        def create(self, model, messages, tools, **kwargs):
            self.calls += 1
            raise TimeoutError("simulated SDK timeout")

    completions = TimingOutCompletions([])
    llm = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = _make_agent(llm)

    result = agent.run("verify something", max_steps=5, max_seconds=0.01)

    assert "超过" in result
    assert completions.calls == 1


def test_max_seconds_reraises_exception_far_from_the_deadline():
    class TimingOutCompletions(FakeCompletions):
        def create(self, model, messages, tools, **kwargs):
            self.calls += 1
            raise RuntimeError("some unrelated failure")

    completions = TimingOutCompletions([])
    llm = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    agent = _make_agent(llm)

    try:
        agent.run("verify something", max_steps=5, max_seconds=100)
        raised = False
    except RuntimeError:
        raised = True

    assert raised
