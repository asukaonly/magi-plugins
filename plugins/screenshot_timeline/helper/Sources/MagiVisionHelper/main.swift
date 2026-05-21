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

while let line = readLine(strippingNewline: true) {
    guard let data = line.data(using: .utf8) else { continue }
    do {
        let req = try JSONDecoder().decode(HelperRequest.self, from: data)
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
            let semaphore = DispatchSemaphore(value: 0)
            Task {
                defer { semaphore.signal() }
                do {
                    let win = try currentActiveWindow()
                    let scope = captureScope(scope: req.scope)
                    let image = try await performCapture(scope: scope)

                    // Files
                    guard let paths = req.savePaths else {
                        writeResponse(.error(id: req.id, code: "BAD_REQUEST", message: "missing save_paths"))
                        return
                    }
                    let qualities = req.jpegQuality ?? JpegQuality(original: 80, thumbnail: 70)
                    let origBytes = try writeJpeg(image: image, to: paths.original, quality: qualities.original)

                    let thumb = resizedJpeg(image: image, maxWidth: req.thumbnailMaxWidth ?? 1024)
                    let thumbBytes = try writeJpeg(image: thumb, to: paths.thumbnail, quality: qualities.thumbnail)

                    // OCR
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
            }
            semaphore.wait()

        default:
            writeResponse(.error(id: req.id, code: "NOT_IMPLEMENTED",
                                 message: "op \(req.op) not implemented yet"))
        }
    } catch {
        writeResponse(.error(id: "unknown", code: "BAD_REQUEST", message: "\(error)"))
    }
}
