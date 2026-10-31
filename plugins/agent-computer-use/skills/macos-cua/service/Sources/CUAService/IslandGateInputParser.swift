import Foundation

enum IslandGateInputParser {
    static func parseLinkedIn(_ raw: String) -> URL? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let withScheme = text.contains("://") ? text : "https://" + text
        guard var parts = URLComponents(string: withScheme), parts.scheme?.lowercased() == "https",
              let host = parts.host?.lowercased(), ["linkedin.com", "www.linkedin.com"].contains(host),
              parts.user == nil, parts.password == nil, parts.port == nil else { return nil }
        let path = parts.path
        guard path.range(of: #"^/posts/[^/]+/?$"#, options: .regularExpression) != nil ||
              path.range(of: #"^/feed/update/urn:li:activity:[0-9]+/?$"#, options: .regularExpression) != nil else { return nil }
        parts.host = "www.linkedin.com"
        return parts.url
    }
    static func parseX(_ raw: String) -> (handle: String, link: URL?)? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        s = s.replacingOccurrences(of: #"^(https?://)?(www\.|mobile\.)?(x|twitter)\.com/"#, with: "", options: [.regularExpression, .caseInsensitive])
        let parts = s.split(separator: "/").map(String.init)
        guard var h = parts.first else { return nil }
        if h.hasPrefix("@") { h.removeFirst() }
        guard h.range(of: #"^[A-Za-z0-9_]{1,15}$"#, options: .regularExpression) != nil else { return nil }
        if parts.count >= 3, parts[1] == "status", parts[2].allSatisfy(\.isNumber) {
            return (h, URL(string: "https://x.com/\(h)/status/\(parts[2])"))
        }
        return parts.count == 1 ? (h, nil) : nil
    }
}
