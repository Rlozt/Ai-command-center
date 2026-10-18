import asyncio

from app.providers.base import AgentResult, Provider, ProviderError, Usage


class ScriptedProvider(Provider):
    """Billable fake provider. `script` is a list of str | ProviderError | callable(req) | 'hang'."""
    billable = True

    def __init__(self, name="openai", role="Analysis", model="gpt-6.1-sol", script=None, in_tok=1000, out_tok=500, cost=None):
        self.name, self.role, self._model = name, role, model
        self.script = list(script or [])
        self.calls = []
        self.in_tok, self.out_tok, self.cost = in_tok, out_tok, cost

    @property
    def model(self):
        return self._model

    def is_configured(self):
        return True

    async def validate(self):
        return True, "ok"

    async def complete(self, req):
        self.calls.append(req)
        item = self.script.pop(0) if len(self.script) > 1 else (self.script[0] if self.script else "{}")
        if item == "hang":
            await asyncio.sleep(3600)
        if isinstance(item, ProviderError):
            raise item
        if callable(item):
            item = item(req)
        return AgentResult(provider=self.name, model=self._model, text=item,
                           usage=Usage(self.in_tok, self.out_tok, self.cost, "reported" if self.cost is not None else "estimated"))
