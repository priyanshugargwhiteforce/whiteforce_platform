TALLY_INSTANCES = {
    1: {
        "name":     "Office",
        "url":      "https://calzone-clumsily-machine.ngrok-free.dev",
        "company":  "Company 1",
        "location": "Main Office",
    },
    2: {
        "name":     "Warehouse",
        "url":      "https://viewless-alongside-cupcake.ngrok-free.dev",
        "company":  "Company 2",
        "location": "Warehouse",
    },
    3: {
        "name":     "Shop",
        "url":      "http://localhost:9000",
        "company":  "Company 3",
        "location": "Shop Floor",
    },
}


from django.shortcuts import get_object_or_404
from tallyapp.models import TallyInstance


def get_instance(tag: str) -> dict:
    try:
        instance = TallyInstance.objects.get(tag=tag, is_active=True)
    except TallyInstance.DoesNotExist:
        valid = list(TallyInstance.objects.filter(is_active=True).values_list("tag", flat=True))
        raise ValueError(f"Tally tag '{tag}' not found. Valid tags: {valid}")
    return instance.to_dict()


def get_all_instances() -> list:
    return [i.to_dict() for i in TallyInstance.objects.filter(is_active=True)]