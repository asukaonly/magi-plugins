import Foundation

let stdin = FileHandle.standardInput
let stdout = FileHandle.standardOutput
let stderr = FileHandle.standardError

func writeResponse(_ resp: HelperResponse) {
    do {
        let data = try JSONEncoder().encode(resp)
        stdout.write(data)
        stdout.write("\n".data(using: .utf8)!)
    } catch {
        stderr.write("encode error: \(error)\n".data(using: .utf8)!)
    }
}

// Handle one request. Runs on the main actor so async work (ScreenCaptureKit
// / NSXPCConnection callbacks) can land on the main RunLoop without
// deadlocking — see the note at the bottom of this file.
@MainActor
func handle(_ req: HelperRequest) async {
    switch req.op {
    case "shutdown":
        writeResponse(.success(id: req.id))
        exit(0)

    case "probe_active_window":
        do {
            let win = try currentActiveWindow()
            writeResponse(.success(id: req.id, activeWindow: win))
        } catch {
            writeResponse(.error(id: req.id, code: "CAPTURE_FAILED", message: "\(error)"))
        }

    case "capture_and_ocr":
        do {
            let win = try currentActiveWindow()
            let scope = captureScope(scope: req.scope)
            let image = try await performCapture(scope: scope)

            guard let paths = req.savePaths else {
                writeResponse(.error(id: req.id, code: "BAD_REQUEST", message: "missing save_paths"))
                return
            }
            let qualities = req.jpegQuality ?? JpegQuality(original: 80, thumbnail: 70)
            let origBytes = try writeJpeg(image: image, to: paths.original, quality: qualities.original)

            let thumb = resizedJpeg(image: image, maxWidth: req.thumbnailMaxWidth ?? 1024)
            let thumbBytes = try writeJpeg(image: thumb, to: paths.thumbnail, quality: qualities.thumbnail)

            let ocrCfg = req.ocr ?? OcrConfig(languages: ["en-US"], level: "accurate")
            let ocrResult = try runOcr(on: image, languages: ocrCfg.languages, level: ocrCfg.level)

            let dims = [image.width, image.height]
            let now = Date().timeIntervalSince1970
            writeResponse(.success(
                id: req.id,
                capturedAt: now,
                dimensions: dims,
                activeWindow: win,
                ocr: ocrResult,
                filesWritten: FilesWritten(originalBytes: origBytes, thumbnailBytes: thumbBytes)
            ))
        } catch CaptureError.permissionDenied {
            writeResponse(.error(id: req.id, code: "PERMISSION_DENIED",
                                 message: "Screen Recording permission not granted"))
        } catch {
            writeResponse(.error(id: req.id, code: "CAPTURE_FAILED", message: "\(error)"))
        }

    case "probe_screen_recording":
        writeResponse(.permission(id: req.id, status: checkScreenRecordingPermission()))

    case "request_screen_recording":
        requestScreenRecordingPermission()
        writeResponse(.permission(id: req.id, status: checkScreenRecordingPermission()))

    case "probe_accessibility":
        writeResponse(.permission(id: req.id, status: checkAccessibilityPermission()))

    case "request_accessibility":
        requestAccessibilityPermission()
        writeResponse(.permission(id: req.id, status: checkAccessibilityPermission()))

    case "probe_screen_lock":
        writeResponse(.screenLock(id: req.id, locked: checkScreenLocked()))

    default:
        writeResponse(.error(id: req.id, code: "NOT_IMPLEMENTED",
                             message: "op \(req.op) not implemented yet"))
    }
}

// Read stdin on a background thread, then bounce each request onto the main
// actor for handling. This serializes requests one-at-a-time (the next
// readLine doesn't fire until the previous handle() resolves), which matches
// the previous semantics.
//
// Architecture note — why a background reader + main RunLoop:
//
// Previous version blocked the main thread in `DispatchSemaphore.wait()` while
// a `Task { ... }` did the capture. That deadlocks: ScreenCaptureKit's XPC
// connection (ReplayKit's RPDaemonProxy) dispatches some callbacks onto the
// main queue while issuing sandbox extensions on first use. With the main
// thread parked in semaphore_wait_trap those callbacks never run, so the
// capture future never completes, so the semaphore never signals.
//
// Fix: main thread runs the RunLoop, background thread blocks on stdin. As a
// bonus, when the parent backend dies, stdin EOFs and we exit cleanly — no
// more orphan helpers (fix #43).
Thread {
    while let line = readLine(strippingNewline: true) {
        guard let data = line.data(using: .utf8) else { continue }
        let req: HelperRequest
        do {
            req = try JSONDecoder().decode(HelperRequest.self, from: data)
        } catch {
            writeResponse(.error(id: "unknown", code: "BAD_REQUEST", message: "\(error)"))
            continue
        }

        // Bounce to the main actor and wait for completion before reading
        // the next request. The main RunLoop is being pumped by the main
        // thread (below), so XPC callbacks can land there freely while we
        // block here on a non-main thread.
        let sem = DispatchSemaphore(value: 0)
        Task { @MainActor in
            await handle(req)
            sem.signal()
        }
        sem.wait()
    }
    // stdin EOF — parent went away. Exit cleanly so we don't orphan.
    exit(0)
}.start()

// Main thread: run the RunLoop forever. AppKit / NSXPCConnection / dispatch
// callbacks all land here.
RunLoop.main.run()
