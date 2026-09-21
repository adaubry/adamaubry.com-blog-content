"""Odoo 19's HTML term splitting, vendored from odoo/tools/translate.py.

Part of Odoo. Copyright Odoo S.A., licensed under the GNU LGPL v3
(https://www.gnu.org/licenses/lgpl-3.0.html). Unmodified apart from the
imports and parse_html raising ValueError instead of Odoo's UserError.

Odoo translates an HTML field term by term: a term is the text of one block
(a paragraph, heading, list item, table cell...), with inline markup kept, or
a translatable attribute such as an image's alt. html_terms() returns the
terms of a value in document order, exactly as Odoo's field.get_trans_terms().
"""
import re

from lxml import etree, html

SKIPPED_ELEMENT_TYPES = (etree._Comment, etree._ProcessingInstruction, etree.CommentBase, etree.PIBase, etree._Entity)
SKIPPED_ELEMENTS = ('script', 'style', 'title')
TRANSLATED_ELEMENTS = {
    'abbr', 'b', 'bdi', 'bdo', 'br', 'cite', 'code', 'data', 'del', 'dfn', 'em',
    'font', 'i', 'ins', 'kbd', 'keygen', 'mark', 'math', 'meter', 'output',
    'progress', 'q', 'ruby', 's', 'samp', 'small', 'span', 'strong', 'sub',
    'sup', 'time', 'u', 'var', 'wbr', 'text', 'select', 'option',
}
TRANSLATED_ATTRS = {
    'string', 'add-label', 'help', 'sum', 'avg', 'confirm', 'placeholder', 'alt', 'title', 'aria-label',
    'aria-keyshortcuts', 'aria-placeholder', 'aria-roledescription', 'aria-valuetext',
    'value_label', 'data-tooltip', 'label', 'confirm-label', 'confirm-title', 'cancel-label',
}
TRANSLATED_ATTRS.update({f't-attf-{attr}' for attr in TRANSLATED_ATTRS})


def is_translatable_attrib(key, node):
    if not key:
        return False
    if 't-call' not in node.attrib and key in TRANSLATED_ATTRS:
        return True
    return key.endswith('.translate')


def is_translatable_attrib_value(node):
    classes = node.attrib.get('class', '').split(' ')
    return (
        (node.tag == 'input' and node.attrib.get('type', 'text') == 'text')
        and 'datetimepicker-input' not in classes
        or (node.tag == 'input' and node.attrib.get('type') == 'hidden')
        and 'o_translatable_input_hidden' in classes
    )


def is_translatable_attrib_text(node):
    return node.tag == 'field' and node.attrib.get('widget', '') == 'url'


avoid_pattern = re.compile(r"\s*<!DOCTYPE", re.IGNORECASE | re.MULTILINE | re.UNICODE)
space_pattern = re.compile(r"[\s\uFEFF]*")
FORMAT_REGEX = re.compile(r'(?:#\{(.+?)\})|(?:\{\{(.+?)\}\})')


def translate_format_string_expression(term, callback):
    expressions = {}
    def add(exp_py):
        index = len(expressions)
        expressions[str(index)] = exp_py
        return '{{%s}}' % index
    term_without_py = FORMAT_REGEX.sub(lambda g: add(g.group(0)), term)
    translated_value = callback(term_without_py)
    if translated_value:
        return FORMAT_REGEX.sub(lambda g: expressions.get(g.group(0)[2:-2], 'None'), translated_value)


_HTML_PARSER = etree.HTMLParser(encoding='utf8')


def parse_html(text):
    try:
        return html.fragment_fromstring(text, parser=_HTML_PARSER)
    except (etree.ParserError, TypeError) as e:
        raise ValueError(f"Error while parsing HTML: {e}") from e


def serialize_html(node):
    return etree.tostring(node, method='html', encoding='unicode')


def translate_xml_node(node, callback, parse, serialize):
    """ Return the translation of the given XML/HTML node.

        :param node:
        :param callback: callback(text) returns translated text or None
        :param parse: parse(text) returns a node (text is unicode)
        :param serialize: serialize(node) returns unicode text
    """

    def nonspace(text):
        """ Return whether ``text`` is a string with non-space characters. """
        return bool(text) and not space_pattern.fullmatch(text)

    def is_force_inline(node):
        """ Return whether ``node`` is marked as it should be translated as
            one term.
        """
        return "o_translate_inline" in node.attrib.get("class", "").split()

    def translatable(node, force_inline=False):
        """ Return whether the given node can be translated as a whole. """
        # Some specific nodes (e.g., text highlights) have an auto-updated DOM
        # structure that makes them impossible to translate.
        # The introduction of a translation `<span>` in the middle of their
        # hierarchy breaks their functionalities. We need to force them to be
        # translated as a whole using the `o_translate_inline` class.
        force_inline = force_inline or is_force_inline(node)
        return (
            (force_inline or node.tag in TRANSLATED_ELEMENTS)
            # Nodes with directives are not translatable. Directives usually
            # start with `t-`, but this prefix is optional for `groups` (see
            # `_compile_directive_groups` which reads `t-groups` and `groups`)
            and not any(key.startswith("t-") or key == 'groups' or key.endswith(".translate") for key in node.attrib)
            and all(translatable(child, force_inline) for child in node)
        )

    def hastext(node, pos=0, force_inline=False):
        """ Return whether the given node contains some text to translate at the
            given child node position.  The text may be before the child node,
            inside it, or after it.
        """
        force_inline = force_inline or is_force_inline(node)
        return (
            # there is some text before node[pos]
            nonspace(node[pos-1].tail if pos else node.text)
            or (
                pos < len(node)
                and translatable(node[pos], force_inline)
                and (
                    any(  # attribute to translate
                        val and (
                            is_translatable_attrib(key, node) or
                            (key == 'value' and is_translatable_attrib_value(node[pos])) or
                            (key == 'text' and is_translatable_attrib_text(node[pos]))
                        )
                        for key, val in node[pos].attrib.items()
                    )
                    # node[pos] contains some text to translate
                    or hastext(node[pos], 0, force_inline)
                    # node[pos] has no text, but there is some text after it
                    or hastext(node, pos + 1, force_inline)
                )
            )
        )

    def process(node):
        """ Translate the given node. """
        if (
            isinstance(node, SKIPPED_ELEMENT_TYPES)
            or node.tag in SKIPPED_ELEMENTS
            or node.get('t-translation', "").strip() == "off"
            or node.tag == 'attribute' and node.get('name') not in ('value', 'text') and not is_translatable_attrib(node.get('name'), node)
            or node.getparent() is None and avoid_pattern.match(node.text or "")
        ):
            return

        pos = 0
        while True:
            # check for some text to translate at the given position
            if hastext(node, pos):
                # move all translatable children nodes from the given position
                # into a <div> element
                div = etree.Element('div')
                div.text = (node[pos-1].tail if pos else node.text) or ''
                while pos < len(node) and translatable(node[pos], is_force_inline(node)):
                    div.append(node[pos])

                # translate the content of the <div> element as a whole
                content = serialize(div)[5:-6]
                original = content.strip()
                translated = callback(original)
                if translated:
                    result = content.replace(original, translated)
                    # <div/> is used to auto fix crapy result
                    result_elem = parse_html(f"<div>{result}</div>")
                    # change the tag to <span/> which is one of TRANSLATED_ELEMENTS
                    # so that 'result_elem' can be checked by translatable and hastext
                    result_elem.tag = 'span'
                    if translatable(result_elem) and hastext(result_elem):
                        div = result_elem
                        if pos:
                            node[pos-1].tail = div.text
                        else:
                            node.text = div.text

                # move the content of the <div> element back inside node
                while len(div) > 0:
                    node.insert(pos, div[0])
                    pos += 1

            if pos >= len(node):
                break

            # node[pos] is not translatable as a whole, process it recursively
            process(node[pos])
            pos += 1

        # translate the attributes of the node
        for key, val in node.attrib.items():
            if nonspace(val):
                if (
                    is_translatable_attrib(key, node) or
                    (key == 'value' and is_translatable_attrib_value(node)) or
                    (key == 'text' and is_translatable_attrib_text(node))
                ):
                    if key.startswith('t-'):
                        value = translate_format_string_expression(val.strip(), callback)
                    else:
                        value = callback(val.strip())
                    node.set(key, value or val)

    process(node)

    return node


def html_translate(callback, value):
    """Translate an HTML value, calling callback(term) for each term."""
    if not value:
        return value
    root = parse_html("<div>%s</div>" % value)
    result = translate_xml_node(root, callback, parse_html, serialize_html)
    return serialize_html(result)[5:-6].replace('\xa0', '&nbsp;')


def html_terms(value):
    """The terms of an HTML value, in document order, duplicates included."""
    terms = []
    html_translate(terms.append, value)
    return terms
