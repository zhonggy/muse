"""注册用的英文姓名池。

来源：
  - 名：SSA 常见名（美国社会保障署新生儿命名统计，前 100）
  - 姓：US Census 2010 常见姓氏（前 100）

muse 的「完成账户创建」页有时会多出「名 / 姓」两个输入框，出现时从这两个池子里
随机组合填进去。
"""
from __future__ import annotations

import random

FIRST_NAMES: tuple[str, ...] = (
    "Michael", "James", "Robert", "John", "David", "William", "Richard",
    "Joseph", "Thomas", "Christopher", "Charles", "Daniel", "Matthew",
    "Anthony", "Mark", "Donald", "Steven", "Andrew", "Paul", "Joshua",
    "Kenneth", "Kevin", "Brian", "George", "Timothy", "Ronald", "Edward",
    "Jason", "Jeffrey", "Ryan", "Jacob", "Gary", "Nicholas", "Eric",
    "Jonathan", "Stephen", "Larry", "Justin", "Scott", "Brandon",
    "Benjamin", "Samuel", "Gregory", "Alexander", "Patrick", "Frank",
    "Raymond", "Jack", "Dennis", "Jerry", "Tyler", "Aaron", "Jose",
    "Adam", "Nathan", "Henry", "Zachary", "Douglas", "Peter", "Kyle",
    "Noah", "Ethan", "Jeremy", "Walter", "Christian", "Keith", "Roger",
    "Terry", "Austin", "Sean", "Gerald", "Carl", "Harold", "Dylan",
    "Arthur", "Lawrence", "Jordan", "Jesse", "Bryan", "Billy", "Bruce",
    "Gabriel", "Joe", "Logan", "Alan", "Juan", "Albert", "Willie",
    "Elijah", "Wayne", "Randy", "Vincent", "Mason", "Roy", "Ralph",
    "Bobby", "Russell", "Bradley", "Philip", "Eugene",
)

LAST_NAMES: tuple[str, ...] = (
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson",
    "Martin", "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez",
    "Clark", "Ramirez", "Lewis", "Robinson", "Walker", "Young", "Allen",
    "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores",
    "Green", "Adams", "Nelson", "Baker", "Hall", "Rivera", "Campbell",
    "Mitchell", "Carter", "Roberts", "Gomez", "Phillips", "Evans",
    "Turner", "Diaz", "Parker", "Cruz", "Edwards", "Collins", "Reyes",
    "Stewart", "Morris", "Morales", "Murphy", "Cook", "Rogers",
    "Gutierrez", "Ortiz", "Morgan", "Cooper", "Peterson", "Bailey",
    "Reed", "Kelly", "Howard", "Ramos", "Kim", "Cox", "Ward",
    "Richardson", "Watson", "Brooks", "Chavez", "Wood", "James",
    "Bennett", "Gray", "Mendoza", "Ruiz", "Hughes", "Price", "Alvarez",
    "Castillo", "Sanders", "Patel", "Myers", "Long", "Ross", "Foster",
    "Jimenez",
)


def random_full_name() -> tuple[str, str]:
    """返回 (名, 姓)，如 ("Michael", "Smith")。"""
    return random.choice(FIRST_NAMES), random.choice(LAST_NAMES)


assert len(FIRST_NAMES) == 100, f"FIRST_NAMES 应为 100 个，实际 {len(FIRST_NAMES)}"
assert len(LAST_NAMES) == 100, f"LAST_NAMES 应为 100 个，实际 {len(LAST_NAMES)}"
