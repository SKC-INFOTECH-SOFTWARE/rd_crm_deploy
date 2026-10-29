"""Append-only recipe versions and exact direct-ingredient scaling."""
from sqlalchemy import select, insert, update, func
from rd_import.codec import dumps, loads
from . import schema as s
from .database import transaction, now
from .rules import StockError, quantity, required, item, convert, exact, ingredient_id
from .setup import audit


def create_version(engine, product_id: int, name: str, output, unit: str,
                   ingredients: list[dict], notes: str, operator: str, active: bool = True) -> int:
    with transaction(engine) as c:
        product = item(c, product_id)
        if product['classification'] != 'finished product':
            raise StockError('Recipe output must be classified as a finished product.')
        output = quantity(output, positive=True)
        convert(c, product_id, output, unit)
        if not ingredients:
            raise StockError('At least one direct ingredient or packaging line is required.')
        clean = []
        seen = set()
        for row in ingredients:
            identifier = ingredient_id(row.get('item_id'))
            if identifier == product_id or identifier in seen:
                raise StockError('Self-consumption and duplicate ingredient items are not allowed.')
            seen.add(identifier)
            q = quantity(row.get('quantity'), positive=True)
            convert(c, identifier, q, row.get('unit'))
            clean.append(dict(item_id=identifier, quantity=q, unit=row['unit']))
        name = required(name, 'Recipe name')
        version = (c.execute(select(func.max(s.recipes.c.version)).where(
            s.recipes.c.product_id == product_id, s.recipes.c.name == name)).scalar() or 0) + 1
        result = c.execute(insert(s.recipes).values(product_id=product_id, name=name, version=version,
            output=output, unit=unit, ingredients_json=dumps(clean), notes=notes, active=int(active),
            operator=required(operator, 'Operator label'), created_at=now())).inserted_primary_key[0]
        audit(c, 'create_recipe_version', operator, dict(recipe_id=result, version=version))
        return result


def set_active(engine, recipe_id: int, active: bool, operator: str) -> None:
    """Availability may change; the formulation and posted snapshots never change."""
    with transaction(engine) as c:
        get_recipe(c, recipe_id)
        c.execute(update(s.recipes).where(s.recipes.c.id == recipe_id).values(active=int(active)))
        audit(c, 'recipe_availability', operator, dict(recipe_id=recipe_id, active=active))


def get_recipe(c, recipe_id: int):
    row = c.execute(select(s.recipes).where(s.recipes.c.id == recipe_id)).mappings().first()
    if row is None:
        raise StockError('Recipe version not found.')
    return row


def scale(c, recipe_id: int, target, unit: str) -> dict:
    recipe = get_recipe(c, recipe_id)
    target = convert(c, recipe['product_id'], quantity(target, positive=True), unit)
    standard = convert(c, recipe['product_id'], recipe['output'], recipe['unit'])
    lines = []
    for row in loads(recipe['ingredients_json']):
        # Multiply before dividing to preserve terminating results (e.g. 3/3).
        expected = exact(lambda: row['quantity'] * target / standard)
        lines.append(dict(item_id=row['item_id'], quantity=expected, unit=row['unit'],
                          base_quantity=convert(c, row['item_id'], expected, row['unit']),
                          base_unit=item(c, row['item_id'])['base_unit']))
    return dict(recipe_id=recipe_id, recipe_name=recipe['name'], version=recipe['version'],
                product_id=recipe['product_id'], output_base_quantity=target,
                output_base_unit=item(c, recipe['product_id'])['base_unit'], ingredients=lines)


def preview(engine, recipe_id: int, target, unit: str) -> dict:
    with engine.connect() as c:
        return scale(c, recipe_id, target, unit)


def detail(engine, recipe_id: int) -> dict:
    with engine.connect() as c:
        return dict(get_recipe(c, recipe_id))
