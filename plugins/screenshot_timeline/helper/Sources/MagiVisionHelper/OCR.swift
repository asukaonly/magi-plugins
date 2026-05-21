import Foundation
import Vision
import CoreGraphics

func runOcr(on image: CGImage, languages: [String], level: String) throws -> OcrResult {
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = (level == "fast") ? .fast : .accurate
    request.recognitionLanguages = languages
    request.usesLanguageCorrection = true
    request.revision = VNRecognizeTextRequestRevision3

    try handler.perform([request])

    let observations = request.results ?? []
    var lines: [String] = []
    var totalConfidence: Float = 0
    var count: Int = 0
    for obs in observations {
        if let cand = obs.topCandidates(1).first {
            lines.append(cand.string)
            totalConfidence += cand.confidence
            count += 1
        }
    }
    let avg = count == 0 ? 0.0 : Double(totalConfidence / Float(count))
    let joined = lines.joined(separator: "\n")
    return OcrResult(text: joined, confidenceAvg: avg, blockCount: lines.count)
}
