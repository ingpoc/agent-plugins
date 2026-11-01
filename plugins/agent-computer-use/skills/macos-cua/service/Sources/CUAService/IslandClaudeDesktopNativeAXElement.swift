import ApplicationServices

/// Lazy bridge from an AXUIElement into the metadata-only Claude reader.
struct IslandClaudeDesktopNativeAXElement: IslandClaudeDesktopActivity.AXElement {
    private let element: AXUIElement

    init(_ element: AXUIElement) { self.element = element }

    var role: String { Self.string(element, kAXRoleAttribute) ?? "" }

    var subrole: String? {
        guard role == "AXOutline" || role == "AXGroup" || role == "AXList" || role == "AXScrollArea" else { return nil }
        return Self.string(element, kAXSubroleAttribute)
    }

    var roleDescription: String? {
        guard ["AXGroup", "AXList", "AXScrollArea"].contains(role) else { return nil }
        return Self.string(element, kAXRoleDescriptionAttribute)
    }

    func labels() -> [String] {
        let role = self.role
        // Groups expose only their accessible name, and only for chat messages ("Currently streaming
        // message", never content) and the complementary landmark (the "Sidebar").
        if role == "AXGroup" {
            return ["AXDocumentArticle", "AXLandmarkComplementary"].contains(subrole ?? "")
                ? [Self.string(element, kAXDescriptionAttribute)].compactMap { $0 } : []
        }
        guard ["AXButton", "AXPopUpButton", "AXRadioButton"].contains(role) else { return [] }
        return [kAXTitleAttribute, kAXDescriptionAttribute].compactMap {
            Self.string(element, $0)
        }
    }

    func isSelected() -> Bool {
        var selected: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXSelectedAttribute as CFString, &selected)
        if selected as? Bool == true { return true }
        // Claude's mode radios report the checked tab as AXValue 1, not AXSelected.
        guard role == "AXRadioButton" else { return false }
        var value: CFTypeRef?
        AXUIElementCopyAttributeValue(element, kAXValueAttribute as CFString, &value)
        return (value as? NSNumber)?.intValue == 1
    }

    func childElements(limit: Int) -> [any IslandClaudeDesktopActivity.AXElement] {
        Self.children(element).prefix(limit).map(IslandClaudeDesktopNativeAXElement.init)
    }

    private static func string(_ element: AXUIElement, _ attribute: String) -> String? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success else { return nil }
        return value as? String
    }

    private static func children(_ element: AXUIElement) -> [AXUIElement] {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, kAXChildrenAttribute as CFString, &value) == .success else { return [] }
        return value as? [AXUIElement] ?? []
    }
}
