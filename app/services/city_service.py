import re

SIMPLE_CITY_NORMALIZATION_MAP = {
    "киев": "Київ",
    "kiev": "Київ",
    "kyiv": "Київ",
    "харьков": "Харків",
    "харькiв": "Харків", 
    "kharkiv": "Харків",
    "одесса": "Одеса",
    "odessa": "Одеса",
    "днепр": "Дніпро",
    "днепропетровск": "Дніпро", 
    "dnipro": "Дніпро",
    "львов": "Львів",
    "lviv": "Львів",
    "запорожье": "Запоріжжя",
    "zaporizhzhia": "Запоріжжя",
    "николаев": "Миколаїв",
    "mykolaiv": "Миколаїв",
    "винница": "Вінниця",
    "vinnytsia": "Вінниця",
    "херсон": "Херсон", 
    "kherson": "Херсон",
    "чернигов": "Чернігів",
    "chernihiv": "Чернігів",
    "полтава": "Полтава",
    "poltava": "Полтава",
    "черкассы": "Черкаси",
    "cherkasy": "Черкаси",
    "хмельницкий": "Хмельницький",
    "khmelnytskyi": "Хмельницький",
    "житомир": "Житомир",
    "zhytomyr": "Житомир",
    "сумы": "Суми",
    "sumy": "Суми",
    "ровно": "Рівне",
    "rivne": "Рівне",
    "ивано-франковск": "Івано-Франківськ",
    "ivano-frankivsk": "Івано-Франківськ",
    "тернополь": "Тернопіль",
    "ternopil": "Тернопіль",
    "луцк": "Луцьк",
    "lutsk": "Луцьк",
    "ужгород": "Ужгород",
    "uzhhorod": "Ужгород",
    "кропивницкий": "Кропивницький",
    "кировоград": "Кропивницький", 
    "kropyvnytskyi": "Кропивницький",
}

def normalize_city_input(city_name: str) -> str:
    if not city_name:
        return ""
    
    processed_name = city_name.strip().lower()
    processed_name = re.sub(r"[^а-яa-zёЁіІїЇєЄґҐ\s-]", "", processed_name, flags=re.IGNORECASE)
    processed_name = re.sub(r"\s+", " ", processed_name).strip()

    if not processed_name:
        return ""

    if processed_name in SIMPLE_CITY_NORMALIZATION_MAP:
        return SIMPLE_CITY_NORMALIZATION_MAP[processed_name]
           
    parts = []
    for part in processed_name.split('-'):
        sub_parts = [word.capitalize() for word in part.split()] 
        parts.append(" ".join(sub_parts))
    final_name = "-".join(parts)
    
    return final_name
