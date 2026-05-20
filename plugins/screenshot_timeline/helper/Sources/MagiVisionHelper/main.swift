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
        default:
            // Stub for capture_and_ocr / probe_active_window — implemented in later tasks.
            writeResponse(.error(id: req.id, code: "NOT_IMPLEMENTED",
                                 message: "op \(req.op) not implemented yet"))
        }
    } catch {
        writeResponse(.error(id: "unknown", code: "BAD_REQUEST", message: "\(error)"))
    }
}
