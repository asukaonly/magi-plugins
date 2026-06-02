import Foundation

public struct OcrConfig: Codable {
    public let languages: [String]
    public let level: String  // "fast" or "accurate"
}

/// Tuning for the AX-first content path. When `enabled`, the helper walks the
/// frontmost focused window's accessibility tree and skips OCR when that tree
/// carries enough in-content text (>= both thresholds). Defaults are biased
/// toward running OCR: the hollow-vs-rich gap is ~100x, so anything between
/// ~20 and ~1000 separates cleanly, and a low bar means borderline windows
/// still get OCR rather than silently losing content.
public struct AXConfig: Codable {
    public let enabled: Bool
    public let wake: Bool             // send AXManualAccessibility to wake Chromium/Electron
    public let minContentChars: Int   // score gate: chars of non-control text
    public let minContentNodes: Int   // score gate: count of non-control text nodes

    enum CodingKeys: String, CodingKey {
        case enabled, wake
        case minContentChars = "min_content_chars"
        case minContentNodes = "min_content_nodes"
    }
}

public struct SavePaths: Codable {
    public let original: String
    public let thumbnail: String
}

public struct JpegQuality: Codable {
    public let original: Int
    public let thumbnail: Int
}

public struct HelperRequest: Codable {
    public let id: String
    public let op: String  // "capture_and_ocr", "probe_active_window", "shutdown"
    public let scope: String?  // "active_window", "full_screen", "display:N"
    public let ocr: OcrConfig?
    public let ax: AXConfig?
    public let savePaths: SavePaths?
    public let jpegQuality: JpegQuality?
    public let thumbnailMaxWidth: Int?

    enum CodingKeys: String, CodingKey {
        case id, op, scope, ocr, ax
        case savePaths = "save_paths"
        case jpegQuality = "jpeg_quality"
        case thumbnailMaxWidth = "thumbnail_max_width"
    }
}

public struct ActiveWindowInfo: Codable {
    public let appBundleId: String
    public let appName: String
    public let windowTitle: String
    public let url: String?
    public let incognito: Bool
    public let displayId: String

    enum CodingKeys: String, CodingKey {
        case appBundleId = "app_bundle_id"
        case appName = "app_name"
        case windowTitle = "window_title"
        case url, incognito
        case displayId = "display_id"
    }
}

public struct OcrResult: Codable {
    public let text: String
    public let confidenceAvg: Double
    public let blockCount: Int

    enum CodingKeys: String, CodingKey {
        case text
        case confidenceAvg = "confidence_avg"
        case blockCount = "block_count"
    }
}

public struct FilesWritten: Codable {
    public let originalBytes: Int
    public let thumbnailBytes: Int

    enum CodingKeys: String, CodingKey {
        case originalBytes = "original_bytes"
        case thumbnailBytes = "thumbnail_bytes"
    }
}

public struct ErrorPayload: Codable {
    public let code: String
    public let message: String
}

public struct HelperResponse: Codable {
    public let id: String
    public let ok: Bool
    public let capturedAt: Double?
    public let dimensions: [Int]?
    public let activeWindow: ActiveWindowInfo?
    public let ocr: OcrResult?
    // AX-first content path. `axText` is exact, reading-order text from the
    // focused window's accessibility tree; `axBlocks` is the capped structured
    // view (role/text/bbox). `axContentChars`/`axContentNodes` are the score
    // the Python side (and the helper's own OCR gate) read to decide whether
    // AX was rich enough. `usedOcrFallback` is true when the score was below
    // threshold (or AX disabled/empty) and OCR was run instead.
    public let axText: String?
    public let axBlocks: [AXBlock]?
    public let axContentChars: Int?
    public let axContentNodes: Int?
    public let axNodeCount: Int?
    public let usedOcrFallback: Bool?
    public let filesWritten: FilesWritten?
    // 64-bit dHash as 16-char lowercase hex. Cheap perceptual fingerprint;
    // hamming distance between two phashes is a strong signal for "same
    // window content, minor pixel difference" (e.g. cursor blink, tiny scroll).
    public let phash: String?
    // System-wide seconds since the last input event of any kind
    // (keyboard, mouse, scroll). Used by the Python sensor's session
    // tracker to decide whether the user has been idle long enough to
    // close the current activity session. Zero macOS permissions required.
    public let idleSeconds: Double?
    public let error: ErrorPayload?
    public let permissionStatus: String?
    public let screenLocked: Bool?

    enum CodingKeys: String, CodingKey {
        case id, ok
        case capturedAt = "captured_at"
        case dimensions
        case activeWindow = "active_window"
        case ocr
        case axText = "ax_text"
        case axBlocks = "ax_blocks"
        case axContentChars = "ax_content_chars"
        case axContentNodes = "ax_content_nodes"
        case axNodeCount = "ax_node_count"
        case usedOcrFallback = "used_ocr_fallback"
        case filesWritten = "files_written"
        case phash
        case idleSeconds = "idle_seconds"
        case error
        case permissionStatus = "permission_status"
        case screenLocked = "screen_locked"
    }

    public static func success(
        id: String,
        capturedAt: Double? = nil,
        dimensions: [Int]? = nil,
        activeWindow: ActiveWindowInfo? = nil,
        ocr: OcrResult? = nil,
        axText: String? = nil,
        axBlocks: [AXBlock]? = nil,
        axContentChars: Int? = nil,
        axContentNodes: Int? = nil,
        axNodeCount: Int? = nil,
        usedOcrFallback: Bool? = nil,
        filesWritten: FilesWritten? = nil,
        phash: String? = nil,
        idleSeconds: Double? = nil
    ) -> HelperResponse {
        HelperResponse(
            id: id, ok: true, capturedAt: capturedAt, dimensions: dimensions,
            activeWindow: activeWindow, ocr: ocr,
            axText: axText, axBlocks: axBlocks, axContentChars: axContentChars,
            axContentNodes: axContentNodes, axNodeCount: axNodeCount,
            usedOcrFallback: usedOcrFallback,
            filesWritten: filesWritten, phash: phash, idleSeconds: idleSeconds,
            error: nil, permissionStatus: nil, screenLocked: nil
        )
    }

    public static func error(id: String, code: String, message: String) -> HelperResponse {
        HelperResponse(
            id: id, ok: false, capturedAt: nil, dimensions: nil,
            activeWindow: nil, ocr: nil,
            axText: nil, axBlocks: nil, axContentChars: nil, axContentNodes: nil,
            axNodeCount: nil, usedOcrFallback: nil,
            filesWritten: nil, phash: nil, idleSeconds: nil,
            error: ErrorPayload(code: code, message: message),
            permissionStatus: nil, screenLocked: nil
        )
    }

    public static func permission(id: String, status: String) -> HelperResponse {
        HelperResponse(
            id: id, ok: true, capturedAt: nil, dimensions: nil,
            activeWindow: nil, ocr: nil,
            axText: nil, axBlocks: nil, axContentChars: nil, axContentNodes: nil,
            axNodeCount: nil, usedOcrFallback: nil,
            filesWritten: nil, phash: nil, idleSeconds: nil, error: nil,
            permissionStatus: status, screenLocked: nil
        )
    }

    public static func screenLock(id: String, locked: Bool) -> HelperResponse {
        HelperResponse(
            id: id, ok: true, capturedAt: nil, dimensions: nil,
            activeWindow: nil, ocr: nil,
            axText: nil, axBlocks: nil, axContentChars: nil, axContentNodes: nil,
            axNodeCount: nil, usedOcrFallback: nil,
            filesWritten: nil, phash: nil, idleSeconds: nil, error: nil,
            permissionStatus: nil, screenLocked: locked
        )
    }
}
