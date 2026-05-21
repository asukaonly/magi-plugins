import Foundation
import CoreGraphics
import ApplicationServices

/// Returns the current Screen Recording TCC status as seen by THIS binary.
/// Apple's API only distinguishes granted vs not — there's no "not_determined"
/// surfacing through CGPreflight.
func checkScreenRecordingPermission() -> String {
    CGPreflightScreenCaptureAccess() ? "granted" : "denied"
}

/// Trigger the Screen Recording permission prompt for THIS binary.
/// No-op if already decided (macOS only prompts once per binary per session).
func requestScreenRecordingPermission() {
    _ = CGRequestScreenCaptureAccess()
}

/// Returns the current Accessibility TCC status as seen by THIS binary.
func checkAccessibilityPermission() -> String {
    AXIsProcessTrusted() ? "granted" : "denied"
}

/// Trigger the Accessibility permission prompt for THIS binary.
func requestAccessibilityPermission() {
    let promptKey = kAXTrustedCheckOptionPrompt.takeRetainedValue() as String
    let options = [promptKey: true] as CFDictionary
    _ = AXIsProcessTrustedWithOptions(options)
}
