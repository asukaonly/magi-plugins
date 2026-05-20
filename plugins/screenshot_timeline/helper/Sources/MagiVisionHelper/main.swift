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
        default:
            // Stub for capture_and_ocr — implemented in later tasks.
            writeResponse(.error(id: req.id, code: "NOT_IMPLEMENTED",
                                 message: "op \(req.op) not implemented yet"))
        }
    } catch {
        writeResponse(.error(id: "unknown", code: "BAD_REQUEST", message: "\(error)"))
    }
}
