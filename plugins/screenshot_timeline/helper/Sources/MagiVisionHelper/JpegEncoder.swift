import Foundation
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers

enum JpegError: Error {
    case writeFailed
}

func writeJpeg(image: CGImage, to path: String, quality: Int) throws -> Int {
    let url = URL(fileURLWithPath: path)
    try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                            withIntermediateDirectories: true)
    guard let dest = CGImageDestinationCreateWithURL(url as CFURL, UTType.jpeg.identifier as CFString, 1, nil) else {
        throw JpegError.writeFailed
    }
    let q = max(0.0, min(1.0, Double(quality) / 100.0))
    let options: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: q]
    CGImageDestinationAddImage(dest, image, options as CFDictionary)
    if !CGImageDestinationFinalize(dest) {
        throw JpegError.writeFailed
    }
    let attr = try FileManager.default.attributesOfItem(atPath: path)
    return (attr[.size] as? Int) ?? 0
}

func resizedJpeg(image: CGImage, maxWidth: Int) -> CGImage {
    let w = image.width
    let h = image.height
    if w <= maxWidth {
        return image
    }
    let ratio = Double(maxWidth) / Double(w)
    let newW = maxWidth
    let newH = Int(Double(h) * ratio)
    let cs = image.colorSpace ?? CGColorSpace(name: CGColorSpace.sRGB)!
    guard let ctx = CGContext(
        data: nil, width: newW, height: newH, bitsPerComponent: 8,
        bytesPerRow: 0, space: cs,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return image
    }
    ctx.interpolationQuality = .high
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: newW, height: newH))
    return ctx.makeImage() ?? image
}
