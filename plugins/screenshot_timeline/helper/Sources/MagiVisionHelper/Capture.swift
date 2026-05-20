import Foundation
import ScreenCaptureKit
import CoreGraphics
import AppKit

enum CaptureScope {
    case activeWindow
    case fullScreen
    case display(id: String)
}

enum CaptureError: Error {
    case permissionDenied
    case noContent
    case captureFailed(String)
}

func captureScope(scope: String?) -> CaptureScope {
    switch scope ?? "active_window" {
    case "full_screen":
        return .fullScreen
    case let s where s.hasPrefix("display:"):
        return .display(id: String(s.dropFirst("display:".count)))
    default:
        return .activeWindow
    }
}

@available(macOS 14.0, *)
func performCapture(scope: CaptureScope) async throws -> CGImage {
    let content: SCShareableContent
    do {
        content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
    } catch {
        throw CaptureError.permissionDenied
    }

    let filter: SCContentFilter
    switch scope {
    case .activeWindow:
        if let frontmost = NSWorkspace.shared.frontmostApplication,
           let win = content.windows.first(where: { $0.owningApplication?.processID == frontmost.processIdentifier && $0.isOnScreen }) {
            filter = SCContentFilter(desktopIndependentWindow: win)
        } else {
            // Fall back to the primary display when no obvious window
            guard let display = content.displays.first else { throw CaptureError.noContent }
            filter = SCContentFilter(display: display, excludingWindows: [])
        }
    case .fullScreen:
        guard let display = content.displays.first else { throw CaptureError.noContent }
        filter = SCContentFilter(display: display, excludingWindows: [])
    case .display(let id):
        let display = content.displays.first(where: { "primary:\($0.displayID)" == id }) ?? content.displays.first
        guard let chosen = display else { throw CaptureError.noContent }
        filter = SCContentFilter(display: chosen, excludingWindows: [])
    }

    let config = SCStreamConfiguration()
    // Conservative defaults — let macOS pick the resolution
    config.showsCursor = false
    config.captureResolution = .best

    let image: CGImage
    do {
        image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
    } catch {
        throw CaptureError.captureFailed("\(error)")
    }
    return image
}
