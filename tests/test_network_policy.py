"""Tests for public/strict network policy (hermes_x402.network_policy)."""

from __future__ import annotations

from hermes_x402.network_policy import NetworkPolicy, parse_network_policy, validate_url_strict

# ---------------------------------------------------------------------------
# Public mode: public hostname accepted
# ---------------------------------------------------------------------------


class TestPublicMode:
    def test_public_hostname_accepted(self):
        policy = NetworkPolicy(mode="public", host_allowlist=(), allow_http=False)
        assert policy.is_url_allowed("https://api.example.com/data")

    def test_public_mode_with_allowlist_rejects_non_matching(self):
        NetworkPolicy(mode="public", host_allowlist=("only.com",), allow_http=False)
        result = validate_url_strict("https://other.com/data", ("only.com",), "public", False)
        assert result is not None

    def test_public_mode_with_allowlist_accepts_matching(self):
        policy = NetworkPolicy(mode="public", host_allowlist=("example.com",), allow_http=False)
        assert policy.is_url_allowed("https://example.com/data")


# ---------------------------------------------------------------------------
# Strict mode
# ---------------------------------------------------------------------------


class TestStrictMode:
    def test_non_allowlisted_hostname_rejected(self):
        policy = NetworkPolicy(
            mode="strict_allowlist", host_allowlist=("allowed.com",), allow_http=False
        )
        assert not policy.is_url_allowed("https://evil.com/steal")

    def test_allowlisted_hostname_accepted(self):
        policy = NetworkPolicy(
            mode="strict_allowlist", host_allowlist=("allowed.com",), allow_http=False
        )
        assert policy.is_url_allowed("https://allowed.com/data")

    def test_subdomain_of_allowlisted_accepted(self):
        policy = NetworkPolicy(
            mode="strict_allowlist", host_allowlist=("example.com",), allow_http=False
        )
        assert policy.is_url_allowed("https://sub.example.com/data")


# ---------------------------------------------------------------------------
# Localhost / loopback rejected in both modes
# ---------------------------------------------------------------------------


class TestLocalhostRejection:
    def test_localhost_rejected_strict(self):
        result = validate_url_strict("https://localhost/secret", (), "strict_allowlist")
        assert result is not None
        assert "blocked" in result.lower()

    def test_localhost_rejected_public(self):
        result = validate_url_strict("https://localhost/secret", (), "public")
        assert result is not None

    def test_loopback_127_rejected(self):
        result = validate_url_strict("https://127.0.0.1/secret", (), "public")
        assert result is not None
        assert "blocked" in result.lower()

    def test_loopback_ipv6_rejected(self):
        result = validate_url_strict("https://[::1]/secret", (), "public")
        assert result is not None


# ---------------------------------------------------------------------------
# RFC1918 private IPs rejected
# ---------------------------------------------------------------------------


class TestPrivateIPRejection:
    def test_10_x_rejected(self):
        result = validate_url_strict("https://10.0.0.1/secret", (), "public")
        assert result is not None
        assert "private" in result.lower()

    def test_192_168_x_rejected(self):
        result = validate_url_strict("https://192.168.1.1/secret", (), "public")
        assert result is not None
        assert "private" in result.lower()

    def test_172_16_x_rejected(self):
        result = validate_url_strict("https://172.16.0.1/secret", (), "public")
        assert result is not None
        assert "private" in result.lower()


# ---------------------------------------------------------------------------
# IPv6 private / link-local rejected
# ---------------------------------------------------------------------------


class TestIPv6Rejection:
    def test_fe80_link_local_rejected(self):
        # Use bracketed IPv6 form so urlparse correctly parses the hostname
        result = validate_url_strict("https://[fe80::1]/secret", (), "public")
        assert result is not None

    def test_fc_prefix_rejected(self):
        result = validate_url_strict("https://[fc00::1]/secret", (), "public")
        assert result is not None

    def test_fd_prefix_rejected(self):
        result = validate_url_strict("https://[fd00::1]/secret", (), "public")
        assert result is not None


# ---------------------------------------------------------------------------
# Metadata IP rejected
# ---------------------------------------------------------------------------


class TestMetadataIP:
    def test_aws_metadata_ip_rejected(self):
        result = validate_url_strict("https://169.254.169.254/latest/meta-data", (), "public")
        assert result is not None
        assert "blocked" in result.lower()

    def test_169_254_prefix_rejected(self):
        result = validate_url_strict("https://169.254.0.1/secret", (), "public")
        assert result is not None
        assert "private" in result.lower()


# ---------------------------------------------------------------------------
# Reserved / multicast / unspecified
# ---------------------------------------------------------------------------


class TestReservedRanges:
    def test_0_0_0_0_unspecified_rejected(self):
        result = validate_url_strict("https://0.0.0.0/secret", (), "public")
        assert result is not None

    def test_224_0_0_1_multicast_rejected(self):
        result = validate_url_strict("https://224.0.0.1/secret", (), "public")
        assert result is not None


# ---------------------------------------------------------------------------
# URL credentials rejected
# ---------------------------------------------------------------------------


class TestCredentialsRejection:
    def test_username_password_rejected(self):
        result = validate_url_strict("https://user:pass@evil.com/secret", (), "public")
        assert result is not None
        assert "credentials" in result.lower()


# ---------------------------------------------------------------------------
# Non-HTTP scheme rejected
# ---------------------------------------------------------------------------


class TestSchemeRejection:
    def test_ftp_rejected(self):
        result = validate_url_strict("ftp://example.com/file", (), "public")
        assert result is not None
        assert "https or http" in result.lower()

    def test_file_rejected(self):
        result = validate_url_strict("file:///etc/passwd", (), "public")
        assert result is not None


# ---------------------------------------------------------------------------
# HTTP vs HTTPS policy
# ---------------------------------------------------------------------------


class TestHTTPPolicy:
    def test_http_rejected_when_allow_http_false(self):
        result = validate_url_strict("http://example.com/data", (), "public", allow_http=False)
        assert result is not None
        assert "http" in result.lower()

    def test_http_accepted_when_allow_http_true(self):
        result = validate_url_strict("http://example.com/data", (), "public", allow_http=True)
        assert result is None

    def test_https_always_accepted(self):
        result = validate_url_strict("https://example.com/data", (), "public", allow_http=False)
        assert result is None


# ---------------------------------------------------------------------------
# Bounded URL length
# ---------------------------------------------------------------------------


class TestURLLength:
    def test_long_url_rejected(self):
        long_url = "https://example.com/" + "a" * 3000
        result = validate_url_strict(long_url, (), "public")
        assert result is not None
        assert "length" in result.lower()

    def test_max_length_accepted(self):
        # Exactly 2048 chars: "https://" + 2040 chars of hostname
        url = "https://" + "a" * 2040
        assert len(url) == 2048
        result = validate_url_strict(url, (), "public")
        assert result is None


# ---------------------------------------------------------------------------
# Empty allowlist behavior
# ---------------------------------------------------------------------------


class TestEmptyAllowlist:
    def test_empty_strict_allows_nothing(self):
        result = validate_url_strict("https://anything.com/x", (), "strict_allowlist")
        assert result is not None
        assert "no hosts" in result.lower()

    def test_empty_public_allows_public_destinations(self):
        result = validate_url_strict("https://example.com/data", (), "public")
        assert result is None


# ---------------------------------------------------------------------------
# validate_destination returns None / error string
# ---------------------------------------------------------------------------


class TestValidateDestination:
    def test_valid_returns_none(self):
        policy = NetworkPolicy(mode="public", host_allowlist=(), allow_http=False)
        assert policy.validate_destination("https://example.com") is None

    def test_invalid_returns_error_string(self):
        policy = NetworkPolicy(mode="strict_allowlist", host_allowlist=(), allow_http=False)
        err = policy.validate_destination("https://example.com")
        assert isinstance(err, str)
        assert len(err) > 0


# ---------------------------------------------------------------------------
# parse_network_policy from env vars
# ---------------------------------------------------------------------------


class TestParseNetworkPolicy:
    def test_default_strict(self, monkeypatch):
        monkeypatch.delenv("X402_NETWORK_POLICY", raising=False)
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        policy = parse_network_policy()
        assert policy.mode == "strict_allowlist"
        assert policy.host_allowlist == ()
        assert policy.allow_http is False

    def test_public_mode(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "public")
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        policy = parse_network_policy()
        assert policy.mode == "public"

    def test_invalid_mode_falls_back_to_strict(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "bogus")
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        policy = parse_network_policy()
        assert policy.mode == "strict_allowlist"

    def test_allowlist_parsed(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "strict_allowlist")
        monkeypatch.setenv("X402_HOST_ALLOWLIST", "a.com, b.com")
        monkeypatch.delenv("X402_ALLOW_HTTP", raising=False)
        policy = parse_network_policy()
        assert policy.host_allowlist == ("a.com", "b.com")

    def test_allow_http_true(self, monkeypatch):
        monkeypatch.setenv("X402_NETWORK_POLICY", "public")
        monkeypatch.delenv("X402_HOST_ALLOWLIST", raising=False)
        monkeypatch.setenv("X402_ALLOW_HTTP", "true")
        policy = parse_network_policy()
        assert policy.allow_http is True
