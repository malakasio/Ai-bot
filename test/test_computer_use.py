"""Integration tests for computer-use MCP server (browser automation).

Tests browser_navigate, browser_click, browser_type, browser_screenshot tools.
Requires jarvis-xvfb Docker container running.
"""

import asyncio
import base64
import os
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from mcp.computer_use_mcp import server


async def test_browser_navigate():
    """Test browser_navigate to example.com."""
    print("\n[TEST] browser_navigate to example.com")

    # Allow example.com for testing
    os.environ["JARVIS_NET_ALLOWLIST"] = "example.com,.example.com"

    result = await server.dispatch("browser_navigate", {
        "url": "https://example.com",
        "wait_for": "load"
    })

    print(f"Result: {result}")

    assert result["ok"] is True, f"Navigation failed: {result}"
    assert result["data"]["status"] == 200, f"Expected HTTP 200, got {result['data']['status']}"
    assert "example.com" in result["data"]["url"].lower(), f"URL mismatch: {result['data']['url']}"
    assert len(result["data"]["title"]) > 0, "Title is empty"

    print(f"✓ Navigated to {result['data']['url']}")
    print(f"✓ Title: {result['data']['title']}")
    print(f"✓ Status: {result['data']['status']}")


async def test_browser_screenshot():
    """Test browser_screenshot after navigation."""
    print("\n[TEST] browser_screenshot")

    result = await server.dispatch("browser_screenshot", {
        "full_page": False
    })

    print(f"Result keys: {result.keys()}")

    assert result["ok"] is True, f"Screenshot failed: {result}"
    assert "screenshot_base64" in result["data"], "No screenshot_base64 in result"
    assert result["data"]["size_bytes"] > 1000, f"Screenshot too small: {result['data']['size_bytes']} bytes"

    # Decode and save screenshot
    screenshot_b64 = result["data"]["screenshot_base64"]
    screenshot_bytes = base64.b64decode(screenshot_b64)

    output_path = "/tmp/jarvis/browser_test.png"
    os.makedirs("/tmp/jarvis", exist_ok=True)
    with open(output_path, "wb") as f:
        f.write(screenshot_bytes)

    print(f"✓ Screenshot captured: {result['data']['size_bytes']} bytes")
    print(f"✓ Saved to: {output_path}")


async def test_browser_click():
    """Test browser_click on example.com link."""
    print("\n[TEST] browser_click")

    # Navigate to a page with clickable elements
    await server.dispatch("browser_navigate", {
        "url": "https://example.com",
        "wait_for": "load"
    })

    # Try to click the "More information..." link
    result = await server.dispatch("browser_click", {
        "selector": "a",
        "button": "left"
    })

    print(f"Result: {result}")

    assert result["ok"] is True, f"Click failed: {result}"
    print(f"✓ Clicked element: {result['data']['selector']}")


async def test_browser_type():
    """Test browser_type (requires input field)."""
    print("\n[TEST] browser_type")

    # Navigate to a page with an input field (using httpbin.org)
    os.environ["JARVIS_NET_ALLOWLIST"] = "example.com,.example.com,httpbin.org,.httpbin.org"

    await server.dispatch("browser_navigate", {
        "url": "https://httpbin.org/forms/post",
        "wait_for": "load"
    })

    # Type into the first input field
    result = await server.dispatch("browser_type", {
        "selector": "input[name='custname']",
        "text": "JARVIS Test",
        "delay_ms": 10
    })

    print(f"Result: {result}")

    assert result["ok"] is True, f"Type failed: {result}"
    assert result["data"]["text_length"] == 11, f"Text length mismatch: {result['data']['text_length']}"
    print(f"✓ Typed {result['data']['text_length']} characters into {result['data']['selector']}")


async def test_security_blocked_url():
    """Test that blocked URLs are rejected."""
    print("\n[TEST] Security: blocked URL")

    # Reset allowlist to only example.com
    os.environ["JARVIS_NET_ALLOWLIST"] = "example.com"

    result = await server.dispatch("browser_navigate", {
        "url": "https://evil.com",
        "wait_for": "load"
    })

    print(f"Result: {result}")

    assert result["ok"] is False, "Expected blocked URL to fail"
    assert "not in JARVIS_NET_ALLOWLIST" in result["error"]["message"], f"Wrong error: {result['error']['message']}"
    print(f"✓ Blocked URL correctly rejected: {result['error']['message']}")


async def test_security_blocked_selector():
    """Test that dangerous selectors are rejected."""
    print("\n[TEST] Security: blocked selector")

    result = await server.dispatch("browser_click", {
        "selector": "javascript:alert(1)",
        "button": "left"
    })

    print(f"Result: {result}")

    assert result["ok"] is False, "Expected blocked selector to fail"
    assert "blocked pattern" in result["error"]["message"], f"Wrong error: {result['error']['message']}"
    print(f"✓ Dangerous selector correctly rejected: {result['error']['message']}")


async def main():
    """Run all tests."""
    print("=" * 70)
    print("JARVIS v7.0 — Computer-Use MCP Server Integration Tests")
    print("=" * 70)

    tests = [
        test_browser_navigate,
        test_browser_screenshot,
        test_browser_click,
        test_browser_type,
        test_security_blocked_url,
        test_security_blocked_selector,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            await test()
            passed += 1
        except AssertionError as e:
            print(f"✗ FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"✗ ERROR: {repr(e)}")
            failed += 1

    print("\n" + "=" * 70)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 70)

    # Cleanup
    from mcp.computer_use_mcp import cleanup
    await cleanup()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
