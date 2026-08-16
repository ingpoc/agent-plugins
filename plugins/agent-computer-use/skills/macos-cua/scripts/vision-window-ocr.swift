import CoreGraphics
import Foundation
import ImageIO
import Vision

struct Frame: Codable {
    let x: Double
    let y: Double
    let w: Double
    let h: Double
}

struct Element: Codable {
    let element_index: Int
    let role: String
    let label: String
    let value: String
    let frame: Frame
}

struct Payload: Codable {
    let source: String
    let tree_markdown: String
    let elements: [Element]
}

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func recognize(
    image: CGImage,
    originX: Double,
    originY: Double,
    logicalWidth: Double,
    logicalHeight: Double,
    maxElements: Int
) throws -> Payload {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = true
    let handler = VNImageRequestHandler(cgImage: image, options: [:])
    try handler.perform([request])
    let observations = request.results ?? []
    var elements: [Element] = []
    for observation in observations.prefix(maxElements) {
        guard let candidate = observation.topCandidates(1).first else { continue }
        let box = observation.boundingBox
        let frame = Frame(
            x: originX + Double(box.minX) * logicalWidth,
            y: originY + (1.0 - Double(box.maxY)) * logicalHeight,
            w: Double(box.width) * logicalWidth,
            h: Double(box.height) * logicalHeight
        )
        elements.append(
            Element(
                element_index: elements.count + 1,
                role: "VisionText",
                label: candidate.string,
                value: "",
                frame: frame
            )
        )
    }
    let tree = elements.map {
        "[\($0.element_index)] \($0.role) \($0.label)"
    }.joined(separator: "\n")
    return Payload(source: "native_vision", tree_markdown: tree, elements: elements)
}

@main
struct Main {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        var maxElements = 120
        if let maxIndex = arguments.firstIndex(of: "--max"), maxIndex + 1 < arguments.count {
            maxElements = Int(arguments[maxIndex + 1]) ?? maxElements
        }

        do {
            let payload: Payload
            if let imageIndex = arguments.firstIndex(of: "--image"), imageIndex + 1 < arguments.count {
                let path = arguments[imageIndex + 1]
                let url = URL(fileURLWithPath: path) as CFURL
                guard
                    let source = CGImageSourceCreateWithURL(url, nil),
                    let image = CGImageSourceCreateImageAtIndex(source, 0, nil)
                else {
                    fail("could not load image: \(path)")
                }
                payload = try recognize(
                    image: image,
                    originX: argumentDouble("--origin-x", in: arguments) ?? 0,
                    originY: argumentDouble("--origin-y", in: arguments) ?? 0,
                    logicalWidth: argumentDouble("--logical-width", in: arguments)
                        ?? Double(image.width),
                    logicalHeight: argumentDouble("--logical-height", in: arguments)
                        ?? Double(image.height),
                    maxElements: maxElements
                )
            } else {
                fail("usage: vision-window-ocr --image PATH [--origin-x X --origin-y Y --logical-width W --logical-height H --max N]")
            }

            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            FileHandle.standardOutput.write(try encoder.encode(payload))
            FileHandle.standardOutput.write(Data("\n".utf8))
        } catch {
            fail("Vision OCR failed: \(error)")
        }
    }
}

func argumentDouble(_ name: String, in arguments: [String]) -> Double? {
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        return nil
    }
    return Double(arguments[index + 1])
}
