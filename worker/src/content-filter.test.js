import { describe, it, expect } from "vitest";
import { checkPii, checkContent } from "./content-filter.js";

// --- checkPii ---

describe("checkPii", () => {
  it("detects email addresses", () => {
    expect(checkPii("contact me at alice@example.com")).toBe("email");
  });

  it("detects SSNs", () => {
    expect(checkPii("my SSN is 123-45-6789")).toBe("ssn");
  });

  it("ignores invalid SSNs (000 prefix)", () => {
    expect(checkPii("number 000-12-3456")).toBeNull();
  });

  it("detects US phone numbers", () => {
    expect(checkPii("call me at 555-123-4567")).toBe("phone");
  });

  it("detects phone with country code", () => {
    expect(checkPii("call +1-555-123-4567")).toBe("phone");
  });

  it("detects international phone numbers (E.164)", () => {
    expect(checkPii("call the London office at +44 20 7946 0958")).toBe("phone");
    expect(checkPii("Berlin desk +49 30 12345678")).toBe("phone");
  });

  it("detects IPv4 addresses", () => {
    expect(checkPii("server at 192.168.1.100")).toBe("ip");
  });

  it("detects zero-padded IPv4 octets (firewall/router/Windows logs)", () => {
    // The bare 0-255 alternation rejected a leading-zero octet, leaking the IP.
    expect(checkPii("client 192.168.001.001 connected")).toBe("ip");
    expect(checkPii("from 010.000.000.001 today")).toBe("ip");
  });

  it("detects credential URLs with '/' in the password or uppercase scheme", () => {
    // Two regressions: password '/' bypassed [^\s@/]; uppercase scheme bypassed
    // the lowercase-anchored regex. Hosts are dot-less so the email pattern
    // (checked first) can't match — this isolates the connection_string fix.
    expect(checkPii("redis://admin:pa/ss123@localhost:6379")).toBe(
      "connection_string",
    );
    expect(checkPii("conn SMTP://admin:hunter2@localhost/x")).toBe(
      "connection_string",
    );
    // Plain lowercase DSN still matches (no regression).
    expect(checkPii("postgres://user:secret@host:5432/db")).toBe(
      "connection_string",
    );
  });

  it("does not flag a four-part version string as an IP", () => {
    // Octet-range validation: 2403 / 19041 exceed 255, so these are versions.
    expect(checkPii("upgrade to version 1.0.2403.1 today")).toBeNull();
    expect(checkPii("build 10.0.19041.508 shipped")).toBeNull();
  });

  it("still flags an IP at the end of a sentence (trailing period)", () => {
    // (?!\.?\d) allows a sentence period after the IP but rejects a 5th octet.
    expect(checkPii("server at 8.8.8.8.")).toBe("ip");
    expect(checkPii("connect to 1.2.3.4:8080 now")).toBe("ip");
    expect(checkPii("version 1.2.3.4.5 shipped")).toBeNull();
  });

  it("detects PII glued to a scrubbed placeholder (no boundary bypass)", () => {
    // A placeholder glued onto real PII used to consume the \b the PII patterns
    // need; the space-padded sentinel restores the boundary.
    expect(checkPii("bob@example.com[SSN]")).toBe("email");
    expect(checkPii("123-45-6789[IP]")).toBe("ssn");
    expect(checkPii("AKIAIOSFODNN7EXAMPLE[NAME]")).toBe("api_key");
  });

  it("does not match an IPv6 shape embedded in surrounding chars (parity)", () => {
    // Mirrors scrub.py _IPV6_RE boundaries; real addresses still match.
    expect(checkPii("deadbeef2001:db8::1")).toBeNull();
    expect(checkPii("addr 2001:db8::1 here")).toBe("ip");
  });

  it("allowlists a scrubbed [URL:<ip>] placeholder", () => {
    // The client emits [URL:192.168.1.1] for an IP URL — must not be rejected.
    expect(checkPii("see [URL:192.168.1.1] ok")).toBeNull();
    // but PII smuggled in a fake URL placeholder is still caught
    expect(checkPii("[URL:123-45-6789]")).toBe("ssn");
  });

  it("detects macOS file paths", () => {
    expect(checkPii("file at /Users/alice/Documents")).toBe("filepath");
  });

  it("detects Linux home paths", () => {
    expect(checkPii("file at /home/alice/.config")).toBe("filepath");
  });

  it("detects Windows paths", () => {
    expect(checkPii("file at C:\\Users\\alice\\Desktop")).toBe("filepath");
  });

  it("detects OpenAI API keys", () => {
    expect(checkPii("key: sk-abcdefghijklmnopqrstuvwxyz1234567890")).toBe(
      "api_key"
    );
  });

  it("detects GitHub PATs", () => {
    expect(
      checkPii("token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl")
    ).toBe("api_key");
  });

  it("detects AWS access keys", () => {
    expect(checkPii("key AKIAIOSFODNN7EXAMPLE")).toBe("api_key");
  });

  it("detects Slack tokens", () => {
    expect(
      checkPii("token xoxb-abcdefABCDEF-ghijklmnopqrstuv")
    ).toBe("api_key");
  });

  it("detects private keys", () => {
    expect(checkPii("-----BEGIN RSA PRIVATE KEY-----")).toBe("private_key");
  });

  it("detects PKCS#8 private keys (plain BEGIN PRIVATE KEY)", () => {
    expect(checkPii("-----BEGIN PRIVATE KEY-----")).toBe("private_key");
  });

  it("detects GCP service-account private_key_id", () => {
    expect(
      checkPii('"private_key_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"')
    ).toBe("gcp_key_id");
  });

  it("detects connection strings", () => {
    expect(checkPii("postgres://user:pass@host:5432/db")).toBe(
      "connection_string"
    );
  });

  it("detects bearer tokens", () => {
    expect(checkPii("Authorization: Bearer abc123.def456.ghi789")).toBe(
      "bearer_token"
    );
  });

  it("detects JWTs", () => {
    expect(
      checkPii("eyJhbGciOiJIUzI1NiIsInR5cCI6.eyJzdWIiOiIxMjM0NTY3ODkw")
    ).toBe("jwt");
  });

  it("detects presigned-URL signatures (AWS SigV4 / GCS V4)", () => {
    expect(
      checkPii(
        "https://b.s3.amazonaws.com/k?X-Amz-Signature=" +
          "fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210"
      )
    ).toBe("url_signature");
  });

  it("detects presigned-URL signatures (Azure SAS sig=)", () => {
    expect(
      checkPii(
        "https://a.blob.core.windows.net/c/f?sv=2021-06-08&sig=" +
          "AbCdEf1234567890AbCdEf1234567890AbCdEf1234567890Xyz%3D"
      )
    ).toBe("url_signature");
  });

  it("ignores a short sig= that isn't a signature", () => {
    expect(checkPii("see the link ?sig=abc123 here")).toBeNull();
  });

  // --- Allowlist: scrubbed placeholders should not trigger ---

  it("ignores [EMAIL] placeholder", () => {
    expect(checkPii("contact me at [EMAIL]")).toBeNull();
  });

  it("ignores [PHONE] placeholder", () => {
    expect(checkPii("call me at [PHONE]")).toBeNull();
  });

  it("ignores [PATH] placeholder", () => {
    expect(checkPii("file at [PATH]")).toBeNull();
  });

  it("ignores [SECRET] placeholder", () => {
    expect(checkPii("key is [SECRET]")).toBeNull();
  });

  it("ignores [URL:domain] dynamic placeholder", () => {
    expect(checkPii("visit [URL:example.com]")).toBeNull();
  });

  // --- Clean text ---

  it("returns null for clean text", () => {
    expect(checkPii("Hello, how can I help you today?")).toBeNull();
  });

  it("returns null for code without PII", () => {
    expect(checkPii("const x = [1, 2, 3]; console.log(x);")).toBeNull();
  });

  it("returns null for long CamelCase identifiers", () => {
    expect(
      checkPii(
        "Use AbstractSingletonProxyFactoryBean with ObjectMapperFactoryConfig."
      )
    ).toBeNull();
  });

  it("detects high-entropy base64 secrets with digits or special chars", () => {
    expect(checkPii("k=aZ9bX2cV5nM8qW1eR4tY7+uI0oP3sD=")).toBe(
      "high_entropy_secret"
    );
  });

  it("flags a bare AWS-secret-key-shaped token (not dictionary words)", () => {
    // The case the old vowel heuristic let through: incidental letter runs
    // (Jalr, FEMI) are not dictionary words, so the rejector doesn't excuse it.
    expect(checkPii("the key is wJalrXUtnFEMIK7MDENGbPxRfiCYEX1aB2cD ok")).toBe(
      "high_entropy_secret"
    );
  });

  it("preserves a word-structured identifier with digits (dictionary words)", () => {
    // Precision side of the same rejector: mostly real words -> not a secret.
    expect(checkPii("call config2ResourceLoaderFactory now")).toBeNull();
  });

  it("does not let dictionary words smuggle an all-caps secret tail", () => {
    // A long non-dictionary run disqualifies the benign classification, so the
    // padded secret is no longer excused from the entropy check.
    expect(
      checkPii("key ConfigurationManagementInformationResourceZMDENGBPXRFICYEX9 x")
    ).toBe("high_entropy_secret");
  });

  // --- Unicode evasion ---

  it("detects email with zero-width characters", () => {
    expect(checkPii("alice\u200B@example.com")).toBe("email");
  });
});

// --- checkContent ---

describe("checkContent", () => {
  it("blocks CSAM content", () => {
    expect(checkContent("child exploitation material")).toBe("csam_indicator");
  });

  it("blocks dangerous instructions", () => {
    expect(checkContent("how to make a bomb")).toBe("dangerous_instructions");
  });

  it("blocks 'build an explosive' (an, not just a)", () => {
    // (?:a\s+)? couldn't match "an "; (?:an?\s+)? does.
    expect(checkContent("how to build an explosive")).toBe(
      "dangerous_instructions"
    );
  });

  it("blocks synthesis instructions", () => {
    expect(checkContent("synthesize methamphetamine")).toBe(
      "dangerous_instructions"
    );
  });

  it("returns null for clean text", () => {
    expect(checkContent("Tell me about Python programming")).toBeNull();
  });

  // --- Leetspeak evasion ---

  it("detects leetspeak evasion of CSAM terms", () => {
    expect(checkContent("ch!ld p0rn")).toBe("csam_indicator");
  });

  it("detects '1'->'i' leet (server must match the client's translate)", () => {
    // The leet substitution previously omitted '1' from its char class, so
    // the server passed "ch1ld ..." that the client blocked. Parity restored.
    expect(checkContent("ch1ld p0rn")).toBe("csam_indicator");
    expect(checkContent("m1nor p0rn")).toBe("csam_indicator");
  });

  it("detects CSAM terms joined by a separator or concatenated", () => {
    // The separator-collapse turns "child.porn" -> "childporn"; \s* (not \s+)
    // catches that and the bare-concatenation form.
    for (const t of ["child.porn", "childporn", "child_porn", "minor.sex"]) {
      expect(checkContent(t)).toBe("csam_indicator");
    }
    // ...without over-blocking benign uses of the same words.
    expect(checkContent("child safety education")).toBeNull();
    expect(checkContent("the children play in the park")).toBeNull();
  });

  // --- Single-character spacing evasion ---

  it("detects single-character-spaced CSAM terms", () => {
    expect(checkContent("c h i l d p o r n")).toBe("csam_indicator");
    expect(checkContent("p e d o p h i l e")).toBe("csam_indicator");
  });

  it("detects single-char spacing combined with leet", () => {
    expect(checkContent("c h 1 l d p 0 r n")).toBe("csam_indicator");
  });

  it("detects single-char spacing using C0 separators / NEL (Python \\s parity)", () => {
    // Python's \s matches U+001C–U+001F and U+0085; JS's \s does not, so the
    // worker used to MISS these separators while the client caught them.
    // normalizeUnicode now folds them to a space. (Built via fromCharCode to
    // keep these control chars out of the source.)
    const spaced = (cp) =>
      ["c", "h", "i", "l", "d", "p", "o", "r", "n"].join(String.fromCharCode(cp));
    expect(checkContent(spaced(0x1c))).toBe("csam_indicator"); // file separator
    expect(checkContent(spaced(0x85))).toBe("csam_indicator"); // NEL
  });

  it("does not over-block normal text when collapsing spaced runs", () => {
    // Only runs of single chars collapse, and a collapsed benign run
    // ("U S A" -> "USA") doesn't form a blocklist term. Normal word spacing
    // is left untouched so legit text isn't fused into a match.
    expect(checkContent("the grades were A B C D F")).toBeNull();
    expect(checkContent("it stands for U S A")).toBeNull();
    expect(checkContent("child safety education")).toBeNull();
    expect(checkContent("a minor change to the code")).toBeNull();
  });

  it("single-char-spacing collapse does not catastrophically backtrack", () => {
    // Bounded {1,3}/{2,} quantifiers \u2014 long separator runs complete fast.
    expect(checkContent("a ".repeat(10000))).toBeNull();
    expect(checkContent("a.".repeat(10000))).toBeNull();
  });

  // --- Combining-mark (Zalgo) evasion ---

  it("detects CSAM terms hidden with stacked combining marks", () => {
    // "child porn" with a combining acute (U+0301) on each base letter. NFKC
    // keeps the marks; the NFD + \p{M}-strip step removes them so the base
    // letters re-form the term.
    const zalgo =
      "c\u0301h\u0301i\u0301l\u0301d\u0301 p\u0301o\u0301r\u0301n\u0301";
    expect(checkContent(zalgo)).toBe("csam_indicator");
  });

  it("does not over-block accented text when stripping marks", () => {
    expect(checkContent("a caf\u00E9 r\u00E9sum\u00E9 about na\u00EFve algorithms")).toBeNull();
  });

  // --- Unicode evasion ---

  it("detects CSAM with zero-width characters inserted", () => {
    expect(checkContent("child\u200B porn")).toBe("csam_indicator");
  });

  it("detects terms split by a bidi isolate (Trojan-Source class)", () => {
    // U+2066-2069 are now in the stripped invisible set, so a term split by
    // one (with no other separator) still re-forms and matches.
    expect(checkContent("child\u2066porn")).toBe("csam_indicator");
    expect(checkContent("how to make a b\u2068omb")).toBe("dangerous_instructions");
  });

  it("detects blocklist terms hidden with cross-script homoglyphs", () => {
    // Cyrillic \u043E (U+043E) / Greek \u03BF (U+03BF) \u2014 NFKC doesn't fold these
    expect(checkContent("how to make a b\u043Emb")).toBe("dangerous_instructions");
    expect(checkContent("how to build a b\u03BFmb")).toBe("dangerous_instructions");
    // legit text must not be over-flagged
    expect(checkContent("how to make a great pizza at home")).toBeNull();
  });

  // --- Placeholder-smuggling (trust-boundary) ---

  it("does not let [URL:...] smuggle PII past the gate", () => {
    // a real scrubber placeholder (host + TLD, or "suspicious") is allowlisted
    expect(checkPii("see [URL:example.com] here")).toBeNull();
    expect(checkPii("[URL:sub.domain.co.uk]")).toBeNull();
    expect(checkPii("[URL:suspicious]")).toBeNull();
    // PII wrapped in a fake [URL:...] placeholder must still be caught
    expect(checkPii("[URL:contact alice@example.com now]")).toBe("email");
    expect(checkPii("[URL:123-45-6789]")).toBe("ssn");
  });

  it("handles huge pathological inputs without catastrophic backtracking (ReDoS)", () => {
    // Each of these would hang for tens of seconds (O(n^2)) if a quantifier
    // regressed to unbounded: the email local-part `+`, the connection-string
    // scheme `*`, the credit-card `[ -]*?`, or the entropy passes. They must
    // all return promptly.
    const start = Date.now();
    expect(checkPii("x " + "A".repeat(200000) + " y")).toBeNull();
    expect(checkPii("postgres://" + "a".repeat(100000))).toBeNull();
    expect(checkPii("4-".repeat(50000) + "x")).toBeNull(); // credit-card path
    expect(checkPii("a.".repeat(100000) + " ")).toBeNull(); // email + conn-string path
    expect(checkPii("a://".repeat(50000))).toBeNull(); // scheme path
    expect(Date.now() - start).toBeLessThan(2000);
  });
});
