"""Descriptor numbers past select()'s FD_SETSIZE ceiling, as long-lived hosts reach them.

``select.select()`` rejects descriptor numbers at or above 1024. The execution
host reaches them in normal operation (live 2026-11-13: 95 open descriptors,
the highest numbered 2402, 35 minutes after start). The fixture occupies every
free descriptor below the ceiling, so the next pipe or file a test creates is
numbered past it. Import the fixture into a test module to use it.
"""
import os

import pytest

SELECT_CEILING = 1024  # FD_SETSIZE of select(2)


@pytest.fixture
def descriptors_past_select_ceiling():
    if os.name != 'posix':
        pytest.skip('POSIX descriptor numbering')
    import resource
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    wanted = 4 * SELECT_CEILING
    if hard != resource.RLIM_INFINITY and hard < wanted:
        pytest.skip(f'descriptor hard limit {hard} is below {wanted}')
    raised = soft != resource.RLIM_INFINITY and soft < wanted
    if raised:
        resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
    held = []
    try:
        while True:
            descriptor = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            if descriptor >= SELECT_CEILING:
                os.close(descriptor)
                break
            held.append(descriptor)
        yield
    finally:
        for descriptor in held:
            os.close(descriptor)
        if raised:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def next_descriptor() -> int:
    """The number the next descriptor this process opens would receive."""
    probe = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
    os.close(probe)
    return probe
