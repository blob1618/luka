"""Backend statements are included independently of model selection."""
from uuid import uuid4

from app.services.assistant_contracts import VerifiedFact


class Presenter:
    def __init__(self):
        self.facts = {}

    def add(self, lines, *, origin='backend_computed', required=True):
        facts = tuple(VerifiedFact(ref='ref_' + uuid4().hex, label='statement', value=line,
                                   origin=origin, required=required) for line in lines)
        self.facts.update((fact.ref, fact) for fact in facts)
        return facts

    def include(self, facts):
        self.facts.update((fact.ref, fact) for fact in facts)

    def render(self, refs=()):
        if len(refs) != len(set(refs)) or any(ref not in self.facts for ref in refs):
            raise ValueError('La presentación usa referencias desconocidas o repetidas')
        selected = list(refs)
        selected.extend(ref for ref, fact in self.facts.items() if fact.required and ref not in selected)
        return '\n'.join('• ' + self.facts[ref].value for ref in selected)
