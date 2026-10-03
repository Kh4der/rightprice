from apps.inventory.catalog import _variation_records


def test_catalog_record_caches_effective_price_categories_version_and_snapshot():
    categories = [
        {
            "type": "CATEGORY",
            "id": "CAT-ALCOHOL",
            "category_data": {"name": "Alcohol", "path_to_root": []},
        },
        {
            "type": "CATEGORY",
            "id": "CAT-SPIRITS",
            "category_data": {
                "name": "Spirits",
                "path_to_root": [{"category_id": "CAT-ALCOHOL", "category_name": "Alcohol"}],
            },
        },
        {
            "type": "CATEGORY",
            "id": "CAT-VODKA",
            "category_data": {
                "name": "Vodka",
                # Square returns parent first and root last.
                "path_to_root": [
                    {"category_id": "CAT-SPIRITS", "category_name": "Spirits"},
                    {"category_id": "CAT-ALCOHOL", "category_name": "Alcohol"},
                ],
            },
        },
        {
            "type": "CATEGORY",
            "id": "CAT-SALE",
            "category_data": {"name": "Weekly Sale", "path_to_root": []},
        },
    ]
    variation = {
        "type": "ITEM_VARIATION",
        "id": "VAR-750",
        "version": 982451653,
        "updated_at": "2026-10-03T12:30:00Z",
        "custom_attribute_values": {"proof": {"string_value": "80"}},
        "future_square_field": {"must": "survive"},
        "item_variation_data": {
            "item_id": "ITEM-VODKA",
            "name": "750 mL",
            "sku": "VODKA-750",
            "pricing_type": "FIXED_PRICING",
            "price_money": {"amount": 1999, "currency": "USD"},
            "track_inventory": True,
            "location_overrides": [
                {
                    "location_id": "OTHER_LOCATION",
                    "price_money": {"amount": 2999, "currency": "USD"},
                },
                {
                    "location_id": "TEST_LOCATION",
                    "pricing_type": "FIXED_PRICING",
                    "price_money": {"amount": 2199, "currency": "USD"},
                },
            ],
        },
    }
    item = {
        "type": "ITEM",
        "id": "ITEM-VODKA",
        "item_data": {
            "name": "House Vodka",
            "reporting_category": {"id": "CAT-VODKA"},
            "categories": [{"id": "CAT-SALE"}, {"id": "CAT-VODKA"}],
            "variations": [
                {
                    **variation,
                    "version": 1,
                    "future_square_field": {"stale": True},
                }
            ],
        },
    }

    record = _variation_records(
        [item, *categories, variation],
        location_id="TEST_LOCATION",
    )["VAR-750"]

    assert record["current_price_cents"] == 2199
    assert record["current_price_currency"] == "USD"
    assert record["pricing_type"] == "FIXED_PRICING"
    assert record["price_from_location_override"] is True
    assert record["catalog_version"] == 982451653
    assert record["reporting_category_id"] == "CAT-VODKA"
    assert record["reporting_category_name"] == "Vodka"
    assert record["category_path"] == ["Alcohol", "Spirits", "Vodka", "Weekly Sale"]
    assert record["catalog_object_snapshot"]["future_square_field"] == {"must": "survive"}
    assert record["catalog_object_snapshot"]["version"] == 982451653
    assert (
        record["catalog_object_snapshot"]["item_variation_data"]["location_overrides"][1][
            "price_money"
        ]["amount"]
        == 2199
    )


def test_catalog_record_falls_back_to_first_category_and_base_price():
    category = {
        "type": "CATEGORY",
        "id": "CAT-GIN",
        "category_data": {
            "name": "Gin",
            "parent_category": {"id": "CAT-SPIRITS"},
        },
    }
    parent = {
        "type": "CATEGORY",
        "id": "CAT-SPIRITS",
        "category_data": {"name": "Liquor"},
    }
    variation = {
        "type": "ITEM_VARIATION",
        "id": "VAR-GIN",
        "version": "27",
        "item_variation_data": {
            "item_id": "ITEM-GIN",
            "name": "1 L",
            "pricing_type": "FIXED_PRICING",
            "price_money": {"amount": 3099, "currency": "usd"},
            "location_overrides": [
                {
                    "location_id": "OTHER_LOCATION",
                    "price_money": {"amount": 1, "currency": "USD"},
                }
            ],
        },
    }
    item = {
        "type": "ITEM",
        "id": "ITEM-GIN",
        "item_data": {
            "name": "House Gin",
            "categories": [{"id": "CAT-GIN"}],
            "variations": [variation],
        },
    }

    record = _variation_records(
        [category, parent, item, variation],
        location_id="TEST_LOCATION",
    )["VAR-GIN"]

    assert record["current_price_cents"] == 3099
    assert record["current_price_currency"] == "USD"
    assert record["price_from_location_override"] is False
    assert record["reporting_category_id"] == "CAT-GIN"
    assert record["reporting_category_name"] == "Gin"
    assert record["category_path"] == ["Liquor", "Gin"]
    assert record["catalog_version"] == 27


def test_pricing_type_override_does_not_claim_price_came_from_location_override():
    variation = {
        "type": "ITEM_VARIATION",
        "id": "VAR-VARIABLE",
        "item_variation_data": {
            "item_id": "ITEM-VARIABLE",
            "pricing_type": "FIXED_PRICING",
            "price_money": {"amount": 1499, "currency": "USD"},
            "location_overrides": [
                {
                    "location_id": "TEST_LOCATION",
                    "pricing_type": "VARIABLE_PRICING",
                }
            ],
        },
    }
    item = {
        "type": "ITEM",
        "id": "ITEM-VARIABLE",
        "item_data": {"name": "Variable Item", "variations": [variation]},
    }

    record = _variation_records(
        [item, variation],
        location_id="TEST_LOCATION",
    )["VAR-VARIABLE"]

    assert record["current_price_cents"] == 1499
    assert record["pricing_type"] == "VARIABLE_PRICING"
    assert record["price_from_location_override"] is False
