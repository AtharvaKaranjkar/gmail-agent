/**
 * DOM Extractor — injected into the page via Playwright.
 *
 * Walks the entire DOM tree and returns:
 *   - An ordered list of interactive elements (with numeric index)
 *   - Text content between interactive elements (for context)
 *   - A selector map so Python can target elements by index
 *
 * Inspired by browser-use's DOM extraction approach.
 */

(function extractDOM() {
    const INTERACTIVE_TAGS = new Set([
        'a', 'button', 'input', 'select', 'textarea', 'option',
        'label', 'details', 'summary'
    ]);

    const INTERACTIVE_ROLES = new Set([
        'button', 'link', 'textbox', 'checkbox', 'radio', 'tab',
        'menuitem', 'option', 'combobox', 'switch', 'slider',
        'searchbox', 'listbox', 'menu', 'menubar', 'tablist',
        'tree', 'treeitem', 'gridcell', 'row'
    ]);

    const INCLUDE_ATTRIBUTES = [
        'id', 'title', 'type', 'name', 'role',
        'aria-label', 'placeholder', 'value', 'alt',
        'aria-expanded', 'data-tooltip', 'href'
    ];

    let elementIndex = 0;
    const selectorMap = {};  // index -> {xpath, tagName, attributes, text}
    const output = [];       // ordered list of {type: 'element'|'text', ...}

    function getXPath(el) {
        if (el.id) return `//*[@id="${el.id}"]`;
        const parts = [];
        let current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE) {
            let idx = 1;
            let sib = current.previousElementSibling;
            while (sib) {
                if (sib.tagName === current.tagName) idx++;
                sib = sib.previousElementSibling;
            }
            const tagLower = current.tagName.toLowerCase();
            parts.unshift(`${tagLower}[${idx}]`);
            current = current.parentElement;
        }
        return '/' + parts.join('/');
    }

    function getCssSelector(el) {
        if (el.id) return `#${CSS.escape(el.id)}`;
        const parts = [];
        let current = el;
        while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.body) {
            let selector = current.tagName.toLowerCase();
            if (current.className && typeof current.className === 'string') {
                const classes = current.className.trim().split(/\s+/).slice(0, 2);
                if (classes.length && classes[0]) {
                    selector += '.' + classes.map(c => CSS.escape(c)).join('.');
                }
            }
            // Add nth-child for uniqueness
            const parent = current.parentElement;
            if (parent) {
                const siblings = Array.from(parent.children).filter(
                    s => s.tagName === current.tagName
                );
                if (siblings.length > 1) {
                    const idx = siblings.indexOf(current) + 1;
                    selector += `:nth-child(${Array.from(parent.children).indexOf(current) + 1})`;
                }
            }
            parts.unshift(selector);
            current = current.parentElement;
        }
        return parts.join(' > ');
    }

    function isVisible(el) {
        if (!el.getBoundingClientRect) return false;
        const rect = el.getBoundingClientRect();
        if (rect.width === 0 && rect.height === 0) return false;
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) === 0) return false;
        return true;
    }

    function isInteractive(el) {
        const tag = el.tagName.toLowerCase();
        if (INTERACTIVE_TAGS.has(tag)) return true;

        const role = el.getAttribute('role');
        if (role && INTERACTIVE_ROLES.has(role)) return true;

        if (el.hasAttribute('onclick') || el.hasAttribute('tabindex')) return true;
        if (el.hasAttribute('contenteditable') && el.getAttribute('contenteditable') !== 'false') return true;

        // Check for click listeners via cursor style
        const style = window.getComputedStyle(el);
        if (style.cursor === 'pointer') return true;

        return false;
    }

    function getElementText(el) {
        // Get direct text, not children's text
        let text = '';
        for (const child of el.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) {
                text += child.textContent.trim() + ' ';
            }
        }
        return text.trim();
    }

    function getFullText(el) {
        return (el.textContent || '').trim().substring(0, 200);
    }

    function getAttributes(el) {
        const attrs = {};
        for (const attr of INCLUDE_ATTRIBUTES) {
            const val = el.getAttribute(attr);
            if (val && val.trim()) {
                // Truncate long values (like hrefs)
                attrs[attr] = val.trim().substring(0, 150);
            }
        }
        return attrs;
    }

    function getViewportInfo() {
        return {
            scrollTop: window.scrollY || document.documentElement.scrollTop,
            scrollHeight: document.documentElement.scrollHeight,
            viewportHeight: window.innerHeight,
            viewportWidth: window.innerWidth,
            url: window.location.href,
            title: document.title
        };
    }

    function walkDOM(node) {
        if (!node) return;

        // Skip hidden containers entirely
        if (node.nodeType === Node.ELEMENT_NODE) {
            const style = window.getComputedStyle(node);
            if (style.display === 'none' || style.visibility === 'hidden') return;
        }

        for (const child of node.childNodes) {
            if (child.nodeType === Node.TEXT_NODE) {
                const text = child.textContent.trim();
                if (text && text.length > 1) {
                    // Collapse whitespace and limit length
                    const clean = text.replace(/\s+/g, ' ').substring(0, 300);
                    output.push({ type: 'text', content: clean });
                }
            } else if (child.nodeType === Node.ELEMENT_NODE) {
                if (isInteractive(child) && isVisible(child)) {
                    const tag = child.tagName.toLowerCase();
                    const attrs = getAttributes(child);
                    const text = getFullText(child);
                    const rect = child.getBoundingClientRect();

                    const entry = {
                        type: 'element',
                        index: elementIndex,
                        tag: tag,
                        text: text.substring(0, 150),
                        attributes: attrs,
                        rect: {
                            x: Math.round(rect.x),
                            y: Math.round(rect.y),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height)
                        }
                    };

                    output.push(entry);

                    selectorMap[elementIndex] = {
                        index: elementIndex,
                        xpath: getXPath(child),
                        cssSelector: getCssSelector(child),
                        tag: tag,
                        text: text.substring(0, 150),
                        attributes: attrs
                    };

                    elementIndex++;

                    // Still walk children of interactive elements
                    // (e.g., a link containing a span with text)
                    // But DON'T re-index nested interactive elements
                    // unless they are independently interactive
                    walkDOM(child);
                } else {
                    walkDOM(child);
                }
            }
        }
    }

    // ── Execute ─────────────────────────────────────────────────────
    walkDOM(document.body);

    const viewport = getViewportInfo();
    const scrollAbove = viewport.scrollTop;
    const scrollBelow = viewport.scrollHeight - viewport.scrollTop - viewport.viewportHeight;

    // Build LLM-friendly text representation
    let llmText = '';
    if (scrollAbove > 0) {
        llmText += `... ${Math.round(scrollAbove)} pixels above — scroll up to see more ...\n\n`;
    }

    for (const item of output) {
        if (item.type === 'text') {
            llmText += item.content + '\n';
        } else {
            // Format: [index]<tag attr1="val1" attr2="val2">visible text</tag>
            let attrStr = '';
            for (const [k, v] of Object.entries(item.attributes)) {
                attrStr += ` ${k}="${v}"`;
            }
            const text = item.text || '';
            llmText += `[${item.index}]<${item.tag}${attrStr}>${text}</${item.tag}>\n`;
        }
    }

    if (scrollBelow > 50) {
        llmText += `\n... ${Math.round(scrollBelow)} pixels below — scroll down to see more ...`;
    }

    return {
        viewport: viewport,
        elementCount: elementIndex,
        selectorMap: selectorMap,
        llmText: llmText,
        rawElements: output
    };
})();
