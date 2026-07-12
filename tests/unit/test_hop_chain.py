"""Round B: multi-hop ProxyCommand rendering. -J is NOT used on purpose:
ssh does not pass command-line options down to ProxyJump sub-connections,
so keys/host-key policy would silently not apply at the hops."""

from weft.adapters.ssh import SSHAdapter


def _adapter(jump):
    return SSHAdapter("s", "target", "/tmp/r", user="u", jump=jump,
                      ssh_opts=["-i", "/k/id"])


def test_single_hop_renders_proxycommand():
    opts = _adapter(["u@bastion:2201"])._jump_opts()
    assert opts[0] == "-o"
    pc = opts[1]
    assert pc.startswith("ProxyCommand=ssh ")
    assert "-p 2201" in pc and "u@bastion" in pc
    assert "-W %h:%p" in pc
    assert "-i /k/id" in pc          # site opts reach the hop


def test_nested_hops_escape_percent_tokens():
    """The outer ssh percent-expands the whole ProxyCommand value; the
    inner hop's %h:%p must be escaped (%%) to survive one level down."""
    pc = _adapter(["u@b1:2201", "u@b2"])._jump_opts()[1]
    # outer level: exactly one live %h:%p (b2's -W, expanded by the
    # destination ssh) and one escaped %%h:%%p (b1's, one level down)
    assert pc.count("%%h:%%p") == 1
    assert pc.count("%h:%p") == 1     # the escaped form is NOT a substring
    assert pc.index("u@b1") < pc.index("u@b2")   # innermost hop first


def test_no_jump_no_proxy():
    assert _adapter([])._jump_opts() == []
    a = _adapter([])
    assert "-o" not in a._ssh_base()[-1]      # destination is last


def test_control_path_distinguishes_chains():
    a = _adapter(["u@b1"])
    b = _adapter(["u@b2"])
    assert a._control_path != b._control_path
