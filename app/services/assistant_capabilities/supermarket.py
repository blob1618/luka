"""Read-only SEPA price comparison capability."""
from pydantic import Field

from app.services.assistant_contracts import FrozenModel
from app.services.assistant_capabilities.common import data, read_tool
from app.services.supermarket_prices import compare_prices, present_comparison


class PriceSearch(FrozenModel):
    products: tuple[str, ...] = Field(min_length=1, max_length=5)
    locality: str = Field(min_length=2, max_length=120, description="Localidad indicada por el usuario, no inferida")
    province: str | None = Field(default=None, max_length=80)


async def search(context, args):
    result = await compare_prices(args.products, locality=args.locality, province=args.province,
        today=context.turn.reference_day)
    return data(context, [result], present_comparison(result, locality=args.locality))


def supermarket_contracts():
    return (read_tool("compare_supermarket_prices", "Consulta precios SEPA de hasta 5 productos por nombre en la localidad indicada. No registra gastos. Si hay varias presentaciones, pedir marca o presentación; nunca pedir EAN al usuario.", PriceSearch, search),)
