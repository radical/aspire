"""Conservative lexical preservation of C# attributes and their declarations."""

import re

from contracts import require


def scheduling_surface(source):
    tokens = []
    offset = 0
    while offset < len(source):
        if source[offset].isspace():
            offset += 1
            continue
        if source.startswith("//", offset):
            end = source.find("\n", offset)
            offset = len(source) if end < 0 else end
            continue
        if source.startswith("/*", offset):
            end = source.find("*/", offset + 2)
            require(end >= 0, "Unterminated C# comment")
            offset = end + 2
            continue
        start = offset
        # Interpolated strings contain nested C# expressions, including comments
        # and quotes. Do not approximate that grammar as a regular string.
        require(source[offset] != "$", "Interpolated C# strings require human candidate review")
        if source.startswith('"""', offset):
            count = len(re.match(r'"+', source[offset:])[0])
            end = source.find('"' * count, offset + count)
            require(end >= 0, "Unterminated C# raw string")
            offset = end + count
        elif source.startswith('@"', offset):
            offset += 2
            while offset < len(source):
                if source.startswith('""', offset):
                    offset += 2
                elif source[offset] == '"':
                    offset += 1
                    break
                else:
                    offset += 1
            else:
                raise ValueError("Unterminated C# verbatim string")
        elif source[offset] in "\"'":
            quote = source[offset]
            offset += 1
            while offset < len(source) and source[offset] != quote:
                offset += 2 if source[offset] == "\\" else 1
            require(offset < len(source), "Unterminated C# string/character")
            offset += 1
        elif source[offset] == "#":
            end = source.find("\n", offset)
            offset = len(source) if end < 0 else end
        else:
            match = re.match(r"@?[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", source[offset:])
            offset += len(match[0])
        tokens.append(source[start:offset])
    declarations = {}
    for index, token in enumerate(tokens):
        if token in {"namespace", "class", "struct", "record", "interface", "enum"}:
            end = index
            while end < len(tokens) and tokens[end] not in {";", "{"}:
                end += 1
            if end < len(tokens) and tokens[end] == "{":
                declarations.setdefault(end, tuple(tokens[index:end]))
    groups = []
    scopes = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "{":
            scopes.append(declarations.get(index))
        elif token == "}":
            require(scopes, "Unbalanced C# declaration scope")
            scopes.pop()
        if token.startswith("#"):
            groups.append(("directive", token))
        if token in {"using", "namespace", "class", "struct", "record", "interface", "enum"}:
            end = index
            while end < len(tokens) and tokens[end] not in {";", "{"}:
                end += 1
            groups.append(("scope/import", tuple(tokens[index:end + 1])))
        if token == "[":
            start = index
            depth = 1
            while depth and index + 1 < len(tokens):
                index += 1
                depth += (tokens[index] == "[") - (tokens[index] == "]")
            require(depth == 0, "Unbalanced C# attribute/bracket section")
            end = index + 1
            # Preserve the following declaration too: moving an identical
            # attribute onto another method/type must not preserve authority.
            while end < len(tokens) and tokens[end] not in {"{", ";"}:
                end += 1
            # Closing braces determine ownership: Outer.Inner.T and Outer.T
            # must not compare equal just because their opening headers match.
            owners = tuple(scope for scope in scopes if scope is not None)
            groups.append(("bracket/declaration", owners, tuple(tokens[start:end + 1])))
        index += 1
    require(not scopes, "Unbalanced C# declaration scope")
    # All bracket sections are retained, not an attribute-name allow-list.
    # This intentionally also rejects some array/indexer edits rather than
    # pretending a partial lexer establishes full C# semantic equivalence.
    return groups
