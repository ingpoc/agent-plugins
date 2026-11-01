import Foundation

enum IslandGateInputParser {
    static func parseLinkedIn(_ raw: String) -> URL? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        if text.range(of: #"^[A-Za-z0-9-]{3,100}$"#, options: .regularExpression) != nil {
            return URL(string: "https://www.linkedin.com/in/\(text.lowercased())/")
        }
        let withScheme = text.contains("://") ? text : "https://" + text
        guard var parts = URLComponents(string: withScheme), parts.scheme?.lowercased() == "https",
              let host = parts.host?.lowercased(), host.range(of: #"^(?:linkedin\.com|www\.linkedin\.com|[a-z]{2}\.linkedin\.com)$"#, options: .regularExpression) != nil,
              parts.user == nil, parts.password == nil, parts.port == nil else { return nil }
        let path = parts.path
        guard path.range(of: #"^/in/[A-Za-z0-9-]{3,100}/?$"#, options: .regularExpression) != nil else { return nil }
        parts.host = "www.linkedin.com"
        if !parts.path.hasSuffix("/") { parts.path += "/" }
        return parts.url
    }
    static func parseX(_ raw: String) -> (handle: String, link: URL?)? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        s = s.replacingOccurrences(of: #"^(?:https?://)?(?:www\.)?(?:x|twitter)\.com/"#, with: "", options: [.regularExpression, .caseInsensitive])
        if s.hasPrefix("@") { s.removeFirst() }
        let parts = s.split(separator: "/").map(String.init)
        guard var h = parts.first else { return nil }
        guard h.range(of: #"^[A-Za-z0-9_]{1,15}$"#, options: .regularExpression) != nil else { return nil }
        if parts.count == 1 { return (h, nil) }
        if parts.count >= 3, parts[1] == "status", parts[2].allSatisfy(\.isNumber) { return (h, nil) }
        return nil
    }
}
