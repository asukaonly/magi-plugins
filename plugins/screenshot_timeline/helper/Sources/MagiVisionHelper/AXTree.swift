import AppKit
import ApplicationServices
import Foundation

// MARK: - Accessibility-tree extraction
//
// Walks the focused window of the frontmost app via the macOS Accessibility
// API and returns exact, structured text — the AX-first half of the
// "AX-first + OCR-fallback" content pipeline.
//
// Why this beats OCR when it's available: the text is what the app itself
// knows (no recognition errors), it carries semantic roles (button vs body
// vs link), and it costs ~0 CPU compared to running Vision on every frame.
//
// The catch — measured empirically across real apps:
//   * Native AppKit (Finder, Calendar): rich immediately.
//   * Chromium/Electron (VS Code, Chrome, Slack, Claude…): expose NOTHING
//     until an assistive client announces itself. Setting the private
//     `AXManualAccessibility` attribute wakes them; they then build a full
//     tree lazily (so the *first* capture after focus may still be hollow —
//     that frame falls back to OCR, the next one gets AX).
//   * Custom-rendered apps (WeChat, QQ, games): stay hollow even after the
//     wake. These are exactly what the OCR fallback exists for.
//
// We never trust "AX returned something". We MEASURE how much real,
// in-content text the window produced (chars + non-control nodes) and let
// the caller gate OCR on that score. Walking from the *focused window*
// (not the app element) is what keeps the system menu bar — a pile of
// AXMenuItem text that would otherwise mask a hollow window — out of the score.

/// One text-bearing node, flattened for downstream structure/entity use.
public struct AXBlock: Codable {
    public let role: String
    public let text: String
    public let bbox: [Int]?  // [x, y, w, h] in global screen points, best-effort

    enum CodingKeys: String, CodingKey {
        case role, text, bbox
    }
}

/// Result of one AX extraction pass over the frontmost focused window.
public struct AXResult {
    public let windowFound: Bool
    public let nodeCount: Int
    public let contentChars: Int   // chars of text on non-control roles (the score)
    public let contentNodes: Int   // count of non-control text nodes (the score)
    public let text: String        // exact text, reading order, newline-joined
    public let blocks: [AXBlock]    // capped structured view
    public let truncated: Bool     // hit a node/block cap

    static let empty = AXResult(
        windowFound: false, nodeCount: 0, contentChars: 0, contentNodes: 0,
        text: "", blocks: [], truncated: false
    )
}

/// Roles whose text is UI chrome (button/menu/tab labels), not document
/// content. Excluded from the content score so a window full of toolbar
/// buttons doesn't read as "has content". Kept deliberately tight — when in
/// doubt a role counts as content (the hollow-vs-rich gap is ~100x, so the
/// score has enormous slack).
private let controlRoles: Set<String> = [
    "AXButton", "AXMenuItem", "AXMenuBarItem", "AXMenuBar", "AXMenu",
    "AXToolbar", "AXPopUpButton", "AXCheckBox", "AXRadioButton",
    "AXTab", "AXTabGroup", "AXScrollBar", "AXDisclosureTriangle",
]

/// Apps we've already sent the `AXManualAccessibility` wake signal to this
/// helper lifetime. We wake once and never block waiting for the tree to
/// build — the capture loop runs every few seconds, so the next frame
/// reaps the now-built tree while this one (if hollow) falls back to OCR.
@MainActor private var wokenPids: Set<pid_t> = []

@MainActor
private func axCopy(_ element: AXUIElement, _ attribute: String) -> AnyObject? {
    var ref: AnyObject?
    let err = AXUIElementCopyAttributeValue(element, attribute as CFString, &ref)
    return err == .success ? ref : nil
}

@MainActor
private func axRole(_ element: AXUIElement) -> String {
    (axCopy(element, kAXRoleAttribute as String) as? String) ?? "?"
}

/// First non-empty of value → title → description, trimmed. `value` is only
/// taken when it's actually a string (sliders etc. return numbers).
@MainActor
private func axText(_ element: AXUIElement) -> String? {
    for attr in [kAXValueAttribute as String, kAXTitleAttribute as String, kAXDescriptionAttribute as String] {
        if let s = axCopy(element, attr) as? String {
            let t = s.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { return t }
        }
    }
    return nil
}

/// Best-effort [x, y, w, h] from the AXPosition/AXSize AXValues.
@MainActor
private func axBBox(_ element: AXUIElement) -> [Int]? {
    guard let posRef = axCopy(element, kAXPositionAttribute as String),
          let sizeRef = axCopy(element, kAXSizeAttribute as String),
          CFGetTypeID(posRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else {
        return nil
    }
    var point = CGPoint.zero
    var size = CGSize.zero
    AXValueGetValue(posRef as! AXValue, .cgPoint, &point)
    AXValueGetValue(sizeRef as! AXValue, .cgSize, &size)
    return [Int(point.x.rounded()), Int(point.y.rounded()),
            Int(size.width.rounded()), Int(size.height.rounded())]
}

@MainActor
private func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    guard let raw = axCopy(element, kAXChildrenAttribute as String) as? [AnyObject] else {
        return []
    }
    // Each entry is an AXUIElement; the CFGetTypeID guard keeps us safe if AX
    // ever hands back something unexpected.
    return raw.compactMap { item in
        CFGetTypeID(item) == AXUIElementGetTypeID() ? (item as! AXUIElement) : nil
    }
}

/// Resolve the window we should read: the focused window, else the main
/// window, else the first window. Returns nil for a genuinely window-less app.
@MainActor
private func focusedWindow(_ axApp: AXUIElement) -> AXUIElement? {
    for attr in [kAXFocusedWindowAttribute as String, kAXMainWindowAttribute as String] {
        if let w = axCopy(axApp, attr), CFGetTypeID(w) == AXUIElementGetTypeID() {
            return (w as! AXUIElement)
        }
    }
    return axChildren(axApp).first { axRole($0) == "AXWindow" }
        ?? (axCopy(axApp, kAXWindowsAttribute as String) as? [AnyObject])?
            .first { CFGetTypeID($0) == AXUIElementGetTypeID() }
            .map { $0 as! AXUIElement }
}

private struct Accumulator {
    var nodeCount = 0
    var contentChars = 0
    var contentNodes = 0
    var lines: [String] = []
    var blocks: [AXBlock] = []
    var truncated = false
}

@MainActor
private func walk(
    _ element: AXUIElement,
    depth: Int,
    into acc: inout Accumulator,
    maxNodes: Int,
    maxDepth: Int,
    maxBlocks: Int
) {
    if acc.nodeCount >= maxNodes {
        acc.truncated = true
        return
    }
    acc.nodeCount += 1

    let role = axRole(element)
    if let text = axText(element) {
        // Exact text, reading order. Dedupe immediate repeats (AX often
        // mirrors a label onto its container and its static-text child).
        if acc.lines.last != text {
            acc.lines.append(text)
        }
        if acc.blocks.count < maxBlocks {
            acc.blocks.append(AXBlock(role: role, text: String(text.prefix(200)), bbox: axBBox(element)))
        } else {
            acc.truncated = true
        }
        if !controlRoles.contains(role) {
            acc.contentNodes += 1
            acc.contentChars += text.count
        }
    }

    if depth >= maxDepth { return }
    for child in axChildren(element) {
        walk(child, depth: depth + 1, into: &acc, maxNodes: maxNodes, maxDepth: maxDepth, maxBlocks: maxBlocks)
    }
}

/// Extract the AX tree of the frontmost app's focused window.
///
/// - `wake`: send `AXManualAccessibility` to Chromium/Electron apps (once per
///   pid). Imposes a small ongoing a11y-tree cost on the *target* app, so we
///   only ever wake the app being captured.
@MainActor
func extractActiveWindowAX(
    wake: Bool = true,
    timeout: Float = 1.0,
    maxNodes: Int = 5000,
    maxDepth: Int = 100,
    maxBlocks: Int = 400
) -> AXResult {
    guard let app = NSWorkspace.shared.frontmostApplication else { return .empty }
    let pid = app.processIdentifier
    let axApp = AXUIElementCreateApplication(pid)
    // Cross-process AX calls block the caller; cap them so a busy target app
    // can never hang the capture loop.
    AXUIElementSetMessagingTimeout(axApp, timeout)

    if wake && !wokenPids.contains(pid) {
        // Private attribute Chromium honors to start serving its a11y tree.
        // Fire-and-forget: we do NOT sleep for the tree to build.
        AXUIElementSetAttributeValue(axApp, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        wokenPids.insert(pid)
    }

    guard let window = focusedWindow(axApp) else {
        return AXResult(windowFound: false, nodeCount: 0, contentChars: 0,
                        contentNodes: 0, text: "", blocks: [], truncated: false)
    }

    var acc = Accumulator()
    walk(window, depth: 0, into: &acc, maxNodes: maxNodes, maxDepth: maxDepth, maxBlocks: maxBlocks)
    return AXResult(
        windowFound: true,
        nodeCount: acc.nodeCount,
        contentChars: acc.contentChars,
        contentNodes: acc.contentNodes,
        text: acc.lines.joined(separator: "\n"),
        blocks: acc.blocks,
        truncated: acc.truncated
    )
}
