import AppKit
import ApplicationServices
import Foundation

enum WindowInfoError: Error {
    case noFrontmostApp
}

func currentActiveWindow() throws -> ActiveWindowInfo {
    guard let app = NSWorkspace.shared.frontmostApplication else {
        throw WindowInfoError.noFrontmostApp
    }
    let bundleId = app.bundleIdentifier ?? "unknown"
    let appName = app.localizedName ?? bundleId

    let (windowTitle, incognito) = frontWindowTitleAndIncognito(pid: app.processIdentifier) ?? ("", false)
    let url = browserURLIfAvailable(bundleId: bundleId)

    let display = primaryDisplayId()

    return ActiveWindowInfo(
        appBundleId: bundleId,
        appName: appName,
        windowTitle: windowTitle,
        url: url,
        incognito: incognito,
        displayId: display
    )
}

private func frontWindowTitleAndIncognito(pid: pid_t) -> (String, Bool)? {
    let appRef = AXUIElementCreateApplication(pid)
    var focused: AnyObject?
    if AXUIElementCopyAttributeValue(appRef, kAXFocusedWindowAttribute as CFString, &focused) != .success {
        return nil
    }
    guard let raw = focused, CFGetTypeID(raw) == AXUIElementGetTypeID() else {
        // Degrade gracefully (empty title) when AX returns something unexpected.
        return nil
    }
    // CFGetTypeID guard above is the safety net — `as?` to a CF type "always succeeds"
    // per the Swift compiler, so we use an unconditional bridge here.
    let windowRef = raw as! AXUIElement
    var titleRef: AnyObject?
    AXUIElementCopyAttributeValue(windowRef, kAXTitleAttribute as CFString, &titleRef)
    let title = (titleRef as? String) ?? ""

    // Heuristic incognito hint from window attributes; we mostly rely on title-pattern in Python guard.
    let lower = title.lowercased()
    let incognito = lower.contains("incognito") || lower.contains("private browsing")
    return (title, incognito)
}

// MARK: - Browser URL via AppleScript
//
// Pulls the *structured* URL from the front browser tab instead of trying to
// OCR the address bar out of pixels. Keys L2/memory off the same identifier
// the chrome-history plugin uses, so "what was I looking at" queries land
// on a precise web_page entity rather than a fuzzy OCR mention.
//
// First use of NSAppleScript against a target app triggers macOS' Automation
// TCC prompt (System Settings → Privacy & Security → Automation). Denial /
// not-yet-granted / missing app are all returned as nil so the capture flow
// continues without a URL field.

/// Cached compiled scripts, keyed by bundle id. NSAppleScript compile is
/// non-trivial; this avoids paying that cost on every capture.
private var _cachedBrowserScripts: [String: NSAppleScript] = [:]

/// Bundles that respond to "URL of active tab of front window" — i.e. all
/// Chromium-derived browsers that kept Chrome's AppleScript dictionary.
private let _chromiumLikeBundleIds: [String: String] = [
    "com.google.Chrome": "Google Chrome",
    "com.google.Chrome.canary": "Google Chrome Canary",
    "com.google.Chrome.beta": "Google Chrome Beta",
    "com.google.Chrome.dev": "Google Chrome Dev",
    "com.microsoft.edgemac": "Microsoft Edge",
    "com.brave.Browser": "Brave Browser",
    "com.brave.Browser.beta": "Brave Browser Beta",
    "com.brave.Browser.nightly": "Brave Browser Nightly",
    "company.thebrowser.Browser": "Arc",
    "com.vivaldi.Vivaldi": "Vivaldi",
]

private func browserURLIfAvailable(bundleId: String) -> String? {
    guard let script = scriptForBrowser(bundleId: bundleId) else {
        return nil
    }
    var errorInfo: NSDictionary?
    let result = script.executeAndReturnError(&errorInfo)
    if errorInfo != nil {
        // Common causes: target app not running, no window, user denied
        // Automation permission, AppleScript dictionary changed. All
        // benign — silently degrade.
        return nil
    }
    guard let value = result.stringValue, !value.isEmpty else {
        return nil
    }
    // Some browsers return "missing value" as a string when there's no
    // active tab (e.g. only a preferences window). Filter that out.
    if value == "missing value" {
        return nil
    }
    return value
}

private func scriptForBrowser(bundleId: String) -> NSAppleScript? {
    if let cached = _cachedBrowserScripts[bundleId] {
        return cached
    }
    let source: String
    switch bundleId {
    case "com.apple.Safari":
        // Safari uses "current tab", not "active tab".
        source = """
        tell application id "com.apple.Safari"
            if (count of windows) > 0 then
                return URL of current tab of front window
            end if
        end tell
        """
    default:
        guard let appName = _chromiumLikeBundleIds[bundleId] else {
            return nil
        }
        // Target the app by its bundle id (more robust than by name when
        // the user has renamed the .app), but Chrome's AppleScript needs
        // the literal name — fall back to that.
        source = """
        tell application "\(appName)"
            if (count of windows) > 0 then
                return URL of active tab of front window
            end if
        end tell
        """
    }
    guard let script = NSAppleScript(source: source) else {
        return nil
    }
    var compileError: NSDictionary?
    script.compileAndReturnError(&compileError)
    if compileError != nil {
        return nil
    }
    _cachedBrowserScripts[bundleId] = script
    return script
}

private func primaryDisplayId() -> String {
    let mainScreen = NSScreen.main
    if let screen = mainScreen,
       let num = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32 {
        return "primary:\(num)"
    }
    return "primary"
}
