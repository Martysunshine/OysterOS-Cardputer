"""--serve keeps polling like the firmware: jobs that arrive after idle polls are executed, transport
errors while serving are ridden out, and idle_exit_s / max_jobs stop it."""

from __future__ import annotations

import httpx
from _simulator_backend import FakeBackend
from oyster_lab.simulator.device import Simulator
from oyster_lab.simulator.scenarios import load_scenario


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _sim(backend: FakeBackend, client: httpx.Client) -> Simulator:
    return Simulator(load_scenario("success"), client, auto_confirm=True, confirm_wait_s=0.0)


def test_serve_executes_jobs_that_arrive_after_idle_polls():
    backend = FakeBackend()
    client = backend.client()
    sim = _sim(backend, client)
    session_id = sim.register_session()
    clock = FakeClock()
    plan = load_scenario("success").plan
    polls = {"n": 0}
    real_poll = sim._poll_job

    def delayed_poll():
        polls["n"] += 1
        if polls["n"] == 3:  # two empty polls first, then the AI's job shows up
            job = dict(plan[0], schema_version="1.0", session_id=session_id, device_id=backend.device_id)
            backend.enqueue(job)
        return real_poll()

    sim._poll_job = delayed_poll
    report = sim.run(serve=True, poll_interval_s=1.0, idle_exit_s=5.0, sleep=clock.sleep, clock=clock)
    client.close()
    assert len(report.jobs) == 1
    assert report.jobs[0].job_id == plan[0]["job_id"]
    assert clock.t >= 5.0  # stopped by the idle timeout after the job, not before it


def test_without_serve_stops_when_queue_is_empty():
    backend = FakeBackend()
    client = backend.client()
    sim = _sim(backend, client)
    sim.register_session()
    report = sim.run()
    client.close()
    assert report.jobs == []


def test_serve_rides_out_transport_errors_and_respects_max_jobs():
    backend = FakeBackend()
    client = backend.client()
    sim = _sim(backend, client)
    session_id = sim.register_session()
    for raw in load_scenario("success").plan[:2]:
        backend.enqueue(dict(raw, schema_version="1.0", session_id=session_id, device_id=backend.device_id))
    clock = FakeClock()
    real_poll = sim._poll_job
    calls = {"n": 0}

    def flaky_poll():
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("backend restarting")
        return real_poll()

    sim._poll_job = flaky_poll
    report = sim.run(serve=True, max_jobs=2, poll_interval_s=0.5, sleep=clock.sleep, clock=clock)
    client.close()
    assert len(report.jobs) == 2
