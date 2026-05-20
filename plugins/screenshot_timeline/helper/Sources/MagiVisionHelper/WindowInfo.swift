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

private func browserURLIfAvailable(bundleId: String) -> String? {
    // Conservative: rely on AX address-field discovery for Safari/Chrome family.
    // For v1, return nil if not easily available — the Python side already records bundle/title.
    return nil
}

private func primaryDisplayId() -> String {
    let mainScreen = NSScreen.main
    if let screen = mainScreen,
       let num = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? UInt32 {
        return "primary:\(num)"
    }
    return "primary"
}
