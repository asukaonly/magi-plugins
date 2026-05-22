import CoreGraphics
import CoreImage
import Foundation

/// Compute a 64-bit difference hash (dHash) of a CGImage.
///
/// Algorithm:
///   1. Resize to 9×8 grayscale.
///   2. For each of the 8 rows, compare each of the 8 (left,right) adjacent
///      pixel pairs — bit = 1 iff right > left.
///   3. Concatenate the 64 bits into a UInt64, return as 16-char lowercase
///      hex.
///
/// Why dHash and not pHash (DCT-based):
///   - Linear-time over a tiny resized image — under 1 ms.
///   - No FFT dependency, no Accelerate matrix code.
///   - Robust to small UI movements (cursor blink, sub-pixel scroll, minor
///      anti-aliasing changes) but sensitive to real content swaps.
///   - 64-bit hex is JSON-safe and trivially compared via popcount.
///
/// Returns nil if the image can't be downsampled (vanishingly rare in
/// practice — we fall through to "no dedup signal" rather than crashing).
public func computeDHash(of image: CGImage) -> String? {
    guard let pixels = downsampleToGrayscale9x8(image: image) else {
        return nil
    }
    var bits: UInt64 = 0
    // 8 rows × 8 comparisons per row. Pixel layout is row-major:
    //   index = row * 9 + col, col ∈ [0, 8].
    var bitIndex = 0
    for row in 0..<8 {
        let base = row * 9
        for col in 0..<8 {
            let left = pixels[base + col]
            let right = pixels[base + col + 1]
            if right > left {
                bits |= (UInt64(1) << (63 - bitIndex))
            }
            bitIndex += 1
        }
    }
    return String(format: "%016llx", bits)
}

/// Draw `image` into a 9×8 grayscale bitmap and return the 72 luminance
/// samples. CoreGraphics handles the resampling — it's a bilinear/lanczos
/// downscale on the GPU when available.
private func downsampleToGrayscale9x8(image: CGImage) -> [UInt8]? {
    let width = 9
    let height = 8
    let colorSpace = CGColorSpaceCreateDeviceGray()
    guard let ctx = CGContext(
        data: nil,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.none.rawValue
    ) else {
        return nil
    }
    ctx.interpolationQuality = .medium
    ctx.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let data = ctx.data else { return nil }
    let buffer = data.assumingMemoryBound(to: UInt8.self)
    return Array(UnsafeBufferPointer(start: buffer, count: width * height))
}

/// Hamming distance between two hex dHash strings (16 chars each).
/// Returns Int.max if either string can't be parsed — callers should
/// treat that as "no dedup signal" rather than "definitely different".
public func hammingDistance(_ a: String, _ b: String) -> Int {
    guard a.count == 16, b.count == 16,
          let lhs = UInt64(a, radix: 16),
          let rhs = UInt64(b, radix: 16) else {
        return Int.max
    }
    return (lhs ^ rhs).nonzeroBitCount
}
