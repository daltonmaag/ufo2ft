from __future__ import (
    print_function, division, absolute_import, unicode_literals
)
from fontTools.misc.py23 import unichr

import collections
import re
try:
    import unicodedata2 as unicodedata
except ImportError:
    import unicodedata
from ufo2ft.featureWriters import BaseFeatureWriter


class KernFeatureWriter(BaseFeatureWriter):
    """Generates a kerning feature based on glyph class definitions.

    Uses the kerning rules contained in an UFO's kerning data, as well as glyph
    classes from parsed feature text. Class-based rules are set based on the
    existing rules for their key glyphs.

    Uses class attributes to match glyph class names in feature text as kerning
    classes, which can be overridden.
    """

    features = ["kern"]
    leftFeaClassRe = r"@MMK_L_(.+)"
    rightFeaClassRe = r"@MMK_R_(.+)"
    options = dict(
        ignoreMarks=True,
    )

    def set_context(self, font):
        ctx = super(KernFeatureWriter, self).set_context(font)

        ctx.kerning = dict(font.kerning)
        ctx.groups = dict(font.groups)

        fealines = []
        if font.features.text:
            for line in font.features.text.splitlines():
                comment_start = line.find('#')
                if comment_start >= 0:
                    line = line[:comment_start]
                line = line.strip()
                if line:
                    fealines.append(line)
        ctx.featxt = '\n'.join(fealines)

        ctx.ltrScripts = collections.OrderedDict()
        ctx.rtlScripts = collections.OrderedDict()
        for script, lang in re.findall(
                r'languagesystem\s+([a-z]{4})\s+([A-Z]+|dflt)\s*;',
                ctx.featxt):
            if self._scriptIsRtl(script):
                ctx.rtlScripts.setdefault(script, []).append(lang)
            else:
                ctx.ltrScripts.setdefault(script, []).append(lang)

        # kerning classes found in existing feature text and UFO groups
        ctx.leftFeaClasses = {}
        ctx.rightFeaClasses = {}
        ctx.leftUfoClasses = {}
        ctx.rightUfoClasses = {}

        # kerning rule collections, mapping pairs to values
        ctx.glyphPairKerning = {}
        ctx.leftClassKerning = {}
        ctx.rightClassKerning = {}
        ctx.classPairKerning = {}

        return ctx

    def _write(self):

        self._collectFeaClasses()
        self._collectFeaClassKerning()

        self._cleanupMissingGlyphs()
        self._correctUfoClassNames()
        self._collectUfoKerning()

        self._removeConflictingKerningRules()

        # write the glyph classes
        lines = []
        self._addGlyphClasses(lines)
        lines.append("")

        # split kerning into LTR and RTL lookups, if necessary
        rtlScripts = self.context.rtlScripts
        if rtlScripts:
            self._splitRtlKerning()

        # write the lookups and feature
        ltrScripts = self.context.ltrScripts
        ltrKern = []
        if ltrScripts or not rtlScripts:
            self._addKerning(ltrKern,
                             self.context.glyphPairKerning)
            self._addKerning(ltrKern,
                             self.context.leftClassKerning,
                             enum=True)
            self._addKerning(ltrKern,
                             self.context.rightClassKerning,
                             enum=True)
            self._addKerning(ltrKern,
                             self.context.classPairKerning,
                             ignoreZero=True)
        if ltrKern:
            lines.append("lookup kern_ltr {")
            if self.options.ignoreMarks:
                lines.append("    lookupflag IgnoreMarks;")
            lines.extend(ltrKern)
            lines.append("} kern_ltr;")
            lines.append("")

        rtlKern = []
        if rtlScripts:
            self._addKerning(rtlKern,
                             self.context.rtlGlyphPairKerning,
                             rtl=True)
            self._addKerning(rtlKern,
                             self.context.rtlLeftClassKerning,
                             rtl=True,
                             enum=True)
            self._addKerning(rtlKern,
                             self.context.rtlRightClassKerning,
                             rtl=True,
                             enum=True)
            self._addKerning(rtlKern,
                             self.context.rtlClassPairKerning,
                             rtl=True,
                             ignoreZero=True)
        if rtlKern:
            lines.append("lookup kern_rtl {")
            if self.options.ignoreMarks:
                lines.append("    lookupflag IgnoreMarks;")
            lines.extend(rtlKern)
            lines.append("} kern_rtl;")
            lines.append("")

        if not (ltrKern or rtlKern):
            # no kerning pairs, don't write empty feature
            return ""

        lines.append("feature kern {")
        if ltrKern:
            lines.append("    lookup kern_ltr;")
        if rtlScripts:
            if ltrKern:
                self._addLookupReferences(lines, ltrScripts, "kern_ltr")
            if rtlKern:
                self._addLookupReferences(lines, rtlScripts, "kern_rtl")
        lines.append("} kern;")

        return self.linesep.join(lines)

    def _collectFeaClasses(self):
        """Parse glyph classes from existing feature text."""

        featxt = self.context.featxt
        leftFeaClasses = self.context.leftFeaClasses
        rightFeaClasses = self.context.rightFeaClasses
        for name, contents in re.findall(
                r'(@[\w.]+)\s*=\s*\[([\s\w.@-]*)\]\s*;', featxt, re.M):
            if re.match(self.leftFeaClassRe, name):
                leftFeaClasses[name] = contents.split()
            elif re.match(self.rightFeaClassRe, name):
                rightFeaClasses[name] = contents.split()

    def _collectFeaClassKerning(self):
        """Set up class kerning rules from class definitions in feature text.

        The first glyph from each class (called it's "key") is used to determine
        the kerning values associated with that class.
        """

        leftFeaClasses = self.context.leftFeaClasses
        rightFeaClasses = self.context.rightFeaClasses
        kerning = self.context.kerning
        classPairKerning = self.context.classPairKerning
        leftClassKerning = self.context.leftClassKerning
        rightClassKerning = self.context.rightClassKerning

        for leftName, leftContents in leftFeaClasses.items():
            leftKey = leftContents[0]

            # collect rules with two classes
            for rightName, rightContents in rightFeaClasses.items():
                rightKey = rightContents[0]
                pair = leftKey, rightKey
                kerningVal = kerning.get(pair)
                if kerningVal is None:
                    continue
                classPairKerning[leftName, rightName] = kerningVal
                del kerning[pair]

            # collect rules with left class and right glyph
            for pair, kerningVal in self._getGlyphKerning(leftKey, 0):
                leftClassKerning[leftName, pair[1]] = kerningVal
                del kerning[pair]

        # collect rules with left glyph and right class
        for rightName, rightContents in rightFeaClasses.items():
            rightKey = rightContents[0]
            for pair, kerningVal in self._getGlyphKerning(rightKey, 1):
                rightClassKerning[pair[0], rightName] = kerningVal
                del kerning[pair]

    def _cleanupMissingGlyphs(self):
        """Removes glyphs missing in the font from groups or kerning pairs."""

        allGlyphs = set(self.context.font.keys())

        groups = {}
        for name, members in self.context.groups.items():
            newMembers = [g for g in members if g in allGlyphs]
            if newMembers:
                groups[name] = newMembers

        kerning = {}
        for glyphPair, val in sorted(self.context.kerning.items()):
            left, right = glyphPair
            if left not in groups and left not in allGlyphs:
                continue
            if right not in groups and right not in allGlyphs:
                continue
            kerning[glyphPair] = val

        self.context.groups = groups
        self.context.kerning = kerning

    def _correctUfoClassNames(self):
        """Detect and replace illegal class names found in UFO kerning."""

        groups = self.context.groups
        kerning = self.context.kerning
        existingClassNames = set()
        for oldName, members in sorted(groups.items()):
            newName = self._makeFeaClassName(oldName, existingClassNames)
            if oldName == newName:
                continue
            groups[newName] = members
            del groups[oldName]
            for oldPair, kerningVal in self._getGlyphKerning(oldName):
                left, right = oldPair
                newPair = (newName, right) if left == oldName else (left, newName)
                kerning[newPair] = kerningVal
                del kerning[oldPair]

    def _collectUfoKerning(self):
        """Sort UFO kerning rules into glyph pair or class rules."""

        groups = self.context.groups
        leftUfoClasses = self.context.leftUfoClasses
        rightUfoClasses = self.context.rightUfoClasses
        classPairKerning = self.context.classPairKerning
        leftClassKerning = self.context.leftClassKerning
        rightClassKerning = self.context.rightClassKerning
        glyphPairKerning = self.context.glyphPairKerning

        for glyphPair, val in sorted(self.context.kerning.items()):
            left, right = glyphPair
            leftIsClass = left in groups
            rightIsClass = right in groups
            if leftIsClass:
                leftUfoClasses[left] = groups[left]
                if rightIsClass:
                    rightUfoClasses[right] = groups[right]
                    classPairKerning[glyphPair] = val
                else:
                    leftClassKerning[glyphPair] = val
            elif rightIsClass:
                rightUfoClasses[right] = groups[right]
                rightClassKerning[glyphPair] = val
            else:
                glyphPairKerning[glyphPair] = val

    def _removeConflictingKerningRules(self):
        """Remove any conflicting pair and class rules.

        If conflicts are detected in a class rule, the offending class members
        are removed from the rule and the class name is replaced with a list of
        glyphs (the class members minus the offending members).
        """

        leftClasses, rightClasses = self._getClasses(separate=True)

        # maintain list of glyph pair rules seen
        seen = dict(self.context.glyphPairKerning)
        liststr = self.liststr

        # remove conflicts in left class / right glyph rules
        leftClassKerning = self.context.leftClassKerning
        for (lClass, rGlyph), val in list(leftClassKerning.items()):
            lGlyphs = leftClasses[lClass]
            nlGlyphs = []
            for lGlyph in lGlyphs:
                pair = lGlyph, rGlyph
                if pair not in seen:
                    nlGlyphs.append(lGlyph)
                    seen[pair] = val
            if nlGlyphs != lGlyphs:
                if nlGlyphs:
                    leftClassKerning[liststr(nlGlyphs), rGlyph] = val
                del leftClassKerning[lClass, rGlyph]

        # remove conflicts in left glyph / right class rules
        rightClassKerning = self.context.rightClassKerning
        for (lGlyph, rClass), val in list(rightClassKerning.items()):
            rGlyphs = rightClasses[rClass]
            nrGlyphs = []
            for rGlyph in rGlyphs:
                pair = lGlyph, rGlyph
                if pair not in seen:
                    nrGlyphs.append(rGlyph)
                    seen[pair] = val
            if nrGlyphs != rGlyphs:
                if nrGlyphs:
                    rightClassKerning[lGlyph, liststr(nrGlyphs)] = val
                del rightClassKerning[lGlyph, rClass]

    def _addGlyphClasses(self, lines):
        """Add glyph classes for the input font's groups."""

        for key, members in sorted(self.context.groups.items()):
            lines.append("@%s = [%s];" % (key, " ".join(members)))

    def _splitRtlKerning(self):
        """Split RTL kerning into separate dictionaries."""

        self.context.rtlGlyphPairKerning = {}
        self.context.rtlLeftClassKerning = {}
        self.context.rtlRightClassKerning = {}
        self.context.rtlClassPairKerning = {}

        classes = self._getClasses()
        allKerning = (
            (self.context.glyphPairKerning,
             self.context.rtlGlyphPairKerning,
             (False, False)),
            (self.context.leftClassKerning,
             self.context.rtlLeftClassKerning,
             (True, False)),
            (self.context.rightClassKerning,
             self.context.rtlRightClassKerning,
             (False, True)),
            (self.context.classPairKerning,
             self.context.rtlClassPairKerning,
             (True, True)))

        for origKerning, rtlKerning, classFlags in allKerning:
            for pair in list(origKerning.keys()):
                allGlyphs = []
                for glyphs, isClass in zip(pair, classFlags):
                    if not isClass:
                        allGlyphs.append(glyphs)
                    elif glyphs.startswith('@'):
                        allGlyphs.extend(classes[glyphs])
                    else:
                        assert glyphs.startswith('[') and glyphs.endswith(']')
                        allGlyphs.extend(glyphs[1:-1].split())
                if any(self._glyphIsRtl(g) for g in allGlyphs):
                    rtlKerning[pair] = origKerning.pop(pair)

    @staticmethod
    def _addKerning(lines, kerning, rtl=False, enum=False,
                    ignoreZero=False):
        """Add kerning rules for a mapping of pairs to values."""

        enum = "enum " if enum else ""
        valstr = "<%(val)d 0 %(val)d 0>" if rtl else "%(val)d"
        lineFormat = "    %spos %%(lhs)s %%(rhs)s %s;" % (enum, valstr)
        for (left, right), val in sorted(kerning.items()):
            if val == 0 and ignoreZero:
                continue
            lines.append(lineFormat % {'lhs': left, 'rhs': right, 'val': val})

    @staticmethod
    def _addLookupReferences(lines, languageSystems, lookupName):
        """Add references to lookup for a set of language systems.

        Language systems are passed in as a dictionary mapping scripts to lists
        of languages.
        """

        for script, langs in languageSystems.items():
            lines.append("script %s;" % script)
            for lang in langs:
                lines.append("language %s;" % lang)
                lines.append("lookup %s;" % lookupName)

    def _getClasses(self, separate=False):
        """Return all kerning classes together."""

        leftClasses = dict(self.context.leftFeaClasses)
        leftClasses.update(self.context.leftUfoClasses)
        rightClasses = dict(self.context.rightFeaClasses)
        rightClasses.update(self.context.rightUfoClasses)
        if separate:
            return leftClasses, rightClasses

        classes = leftClasses
        classes.update(rightClasses)
        return classes

    def _makeFeaClassName(self, name, existingClassNames):
        """Make a glyph class name which is legal to use in feature text.

        Ensures the name only includes characters in "A-Za-z0-9._", and
        isn't already defined.
        """

        name = re.sub(r"[^A-Za-z0-9._]", r"", name)
        i = 1
        origName = name
        while name in existingClassNames:
            name = "%s_%d" % (origName, i)
            i += 1
        existingClassNames.add(name)
        return name

    def _getGlyphKerning(self, glyphName, i=None):
        """Return the kerning rules which include glyphName, optionally only
        checking one side of each pair if index `i` is provided.
        """

        hits = []
        for pair, value in self.context.kerning.items():
            if (glyphName in pair) if i is None else (pair[i] == glyphName):
                hits.append((pair, value))
        return hits

    @staticmethod
    def _scriptIsRtl(script):
        """Return whether a script is right-to-left for kerning purposes.

        References:
        https://github.com/Tarobish/Jomhuria/blob/a21c41453ea8e3893e003ae9d5bee9ba7ac42d77/tools/getKernFeatureFromUFO.py#L18
        https://github.com/behdad/harfbuzz/blob/691086f131cb6c9d97e98730c27673484bf93f87/src/hb-common.cc#L446
        http://unicode.org/iso15924/iso15924-codes.html
        """

        return script in (
            # Unicode-1.1 additions
            'arab',  # ARABIC
            'hebr',  # HEBREW

            # Unicode-3.0 additions
            'syrc',  # SYRIAC
            'thaa',  # THAANA

            # Unicode-4.0 additions
            'cprt',  # CYPRIOT

            # Unicode-4.1 additions
            'khar',  # KHAROSHTHI

            # Unicode-5.0 additions
            'phnx',  # PHOENICIAN
            'nkoo',  # NKO

            # Unicode-5.1 additions
            'lydi',  # LYDIAN

            # Unicode-5.2 additions
            'avst',  # AVESTAN
            'armi',  # IMPERIAL_ARAMAIC
            'phli',  # INSCRIPTIONAL_PAHLAVI
            'prti',  # INSCRIPTIONAL_PARTHIAN
            'sarb',  # OLD_SOUTH_ARABIAN
            'orkh',  # OLD_TURKIC
            'samr',  # SAMARITAN

            # Unicode-6.0 additions
            'mand',  # MANDAIC

            # Unicode-6.1 additions
            'merc',  # MEROITIC_CURSIVE
            'mero',  # MEROITIC_HIEROGLYPHS

            # Unicode-7.0 additions
            'mani',  # MANICHAEAN
            'mend',  # MENDE_KIKAKUI
            'nbat',  # NABATAEAN
            'narb',  # OLD_NORTH_ARABIAN
            'palm',  # PALMYRENE
            'phlp',  # PSALTER_PAHLAVI

            # Unicode-8.0 additions
            'hung',  # OLD_HUNGARIAN

            # Unicode-9.0 additions
            'adlm',  # ADLAM
        )

    def _glyphIsRtl(self, name):
        """Return whether the closest-associated unicode character is RTL."""

        font = self.context.font
        delims = ('.', '_')
        uv = font[name].unicode
        while uv is None and any(d in name for d in delims):
            name = name[:max(name.rfind(d) for d in delims)]
            if name in font:
                uv = font[name].unicode
        if uv is None:
            return False
        return unicodedata.bidirectional(unichr(uv)) in ('R', 'AL')
