import XCTest
@testable import MagiVisionHelper

final class ProtocolTests: XCTestCase {
    func testDecodeCaptureRequest() throws {
        let json = """
        {
          "id": "req_1",
          "op": "capture_and_ocr",
          "scope": "active_window",
          "ocr": {"languages": ["en-US", "zh-Hans"], "level": "accurate"},
          "save_paths": {"original": "/tmp/a.jpg", "thumbnail": "/tmp/a_thumb.jpg"},
          "jpeg_quality": {"original": 80, "thumbnail": 70},
          "thumbnail_max_width": 1024
        }
        """.data(using: .utf8)!
        let req = try JSONDecoder().decode(HelperRequest.self, from: json)
        XCTAssertEqual(req.id, "req_1")
        XCTAssertEqual(req.op, "capture_and_ocr")
        XCTAssertEqual(req.scope, "active_window")
        XCTAssertEqual(req.ocr?.languages, ["en-US", "zh-Hans"])
        XCTAssertEqual(req.savePaths?.original, "/tmp/a.jpg")
        XCTAssertEqual(req.thumbnailMaxWidth, 1024)
    }

    func testEncodeSuccessResponse() throws {
        let resp = HelperResponse.success(
            id: "req_1",
            capturedAt: 100.0,
            dimensions: [1920, 1200],
            activeWindow: ActiveWindowInfo(
                appBundleId: "com.apple.Safari",
                appName: "Safari",
                windowTitle: "Magi",
                url: nil,
                incognito: false,
                displayId: "primary"
            ),
            ocr: OcrResult(text: "hello", confidenceAvg: 0.9, blockCount: 1),
            filesWritten: FilesWritten(originalBytes: 1234, thumbnailBytes: 567)
        )
        let data = try JSONEncoder().encode(resp)
        let s = String(data: data, encoding: .utf8)!
        XCTAssertTrue(s.contains("\"ok\":true"))
        XCTAssertTrue(s.contains("\"captured_at\":100"))
    }

    func testEncodeErrorResponse() throws {
        let resp = HelperResponse.error(id: "req_1", code: "PERMISSION_DENIED", message: "denied")
        let data = try JSONEncoder().encode(resp)
        let s = String(data: data, encoding: .utf8)!
        XCTAssertTrue(s.contains("\"ok\":false"))
        XCTAssertTrue(s.contains("\"code\":\"PERMISSION_DENIED\""))
    }

    func testDecodeCaptureRequestWithAXConfig() throws {
        let json = """
        {
          "id": "req_2",
          "op": "capture_and_ocr",
          "ax": {"enabled": true, "wake": true, "min_content_chars": 80, "min_content_nodes": 5}
        }
        """.data(using: .utf8)!
        let req = try JSONDecoder().decode(HelperRequest.self, from: json)
        XCTAssertEqual(req.ax?.enabled, true)
        XCTAssertEqual(req.ax?.wake, true)
        XCTAssertEqual(req.ax?.minContentChars, 80)
        XCTAssertEqual(req.ax?.minContentNodes, 5)
    }

    func testCaptureRequestWithoutAXDecodesToNil() throws {
        // Back-compat: a request that predates the AX path must still decode.
        let json = """
        {"id": "req_3", "op": "capture_and_ocr"}
        """.data(using: .utf8)!
        let req = try JSONDecoder().decode(HelperRequest.self, from: json)
        XCTAssertNil(req.ax)
    }

    func testEncodeSuccessResponseWithAX() throws {
        let resp = HelperResponse.success(
            id: "req_2",
            axText: "hello\nworld",
            axBlocks: [AXBlock(role: "AXStaticText", text: "hello", bbox: [1, 2, 3, 4])],
            axContentChars: 11,
            axContentNodes: 2,
            axNodeCount: 5,
            usedOcrFallback: false
        )
        let data = try JSONEncoder().encode(resp)
        let s = String(data: data, encoding: .utf8)!
        XCTAssertTrue(s.contains("\"ax_content_chars\":11"))
        XCTAssertTrue(s.contains("\"used_ocr_fallback\":false"))
        XCTAssertTrue(s.contains("\"ax_blocks\""))
    }
}
