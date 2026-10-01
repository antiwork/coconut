"""Structure of the saved work graph that gated PR 3170 behind PR 383 setup work.

Captured read-only from the live database on 2026-10-01. Task IDs keep their first eight characters, PR URLs
became target and sibling, and every other field was dropped: only the edges that _work_chain follows remain.
All tasks share the Desk intake exchange. bad1ea8a is the PR 383 setup execution; the test restores its historical
state, a needs_you task holding a completed, undelivered wait on da8d0428.
"""

TASKS = [
    {'id': 'b8057ab9', 'bot': 'Desk', 'status': 'completed', 'execution': 'b8057ab9', 'reported': ['sibling', 'target']},
    {'id': 'a43e8fe6', 'bot': 'Gumroad Dev', 'status': 'completed', 'source': 'b8057ab9', 'execution': 'a43e8fe6', 'relay': True, 'reported': ['sibling']},
    {'id': '34d9ba7b', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9', 'relay': True, 'reported': ['sibling']},
    {'id': 'f21ea9d7', 'bot': 'Gumroad Dev', 'status': 'completed', 'source': '34d9ba7b', 'requester': '34d9ba7b', 'execution': 'f21ea9d7', 'relay': True, 'reported': ['target']},
    {'id': '5d1c1c34', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9', 'relay': True, 'reported': ['sibling']},
    {'id': '73e5c6bb', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9', 'relay': True, 'reported': ['sibling']},
    {'id': '393ab0b2', 'bot': 'Desk', 'status': 'completed', 'source': 'f21ea9d7', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'f21ea9d7', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True, 'reported': ['target']},
    {'id': '63ec7a8d', 'bot': 'Desk', 'status': 'completed', 'source': 'f21ea9d7', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'f21ea9d7', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True, 'reported': ['target']},
    {'id': '7141aa74', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9', 'relay': True, 'reported': ['sibling']},
    {'id': 'bad1ea8a', 'bot': 'Gumroad Dev', 'status': 'completed', 'source': '7141aa74', 'requester': '7141aa74', 'native': True, 'execution': 'bad1ea8a', 'relay': True, 'reported': ['sibling']},
    {'id': '22984704', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9'},
    {'id': '29a01294', 'bot': 'Gumroad Dev', 'status': 'completed', 'source': '22984704', 'execution': '29a01294', 'relay': True, 'reported': ['sibling']},
    {'id': '68410a4e', 'bot': 'Desk', 'status': 'completed', 'source': 'a43e8fe6', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'a43e8fe6', 'execution': 'b8057ab9'},
    {'id': '3154b9eb', 'bot': 'Desk', 'status': 'completed', 'source': 'bad1ea8a', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'bad1ea8a', 'callback': '7141aa74', 'execution': 'b8057ab9', 'relay': True},
    {'id': '1153615c', 'bot': 'Desk', 'status': 'completed', 'source': '29a01294', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': '29a01294', 'execution': 'b8057ab9', 'relay': True},
    {'id': '5c814b4a', 'bot': 'Desk', 'status': 'completed', 'source': 'bad1ea8a', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'bad1ea8a', 'callback': '7141aa74', 'execution': 'b8057ab9', 'relay': True, 'reported': ['sibling']},
    {'id': 'da8d0428', 'bot': 'Coconut Care', 'status': 'completed', 'source': 'bad1ea8a', 'requester': 'bad1ea8a', 'execution': 'da8d0428', 'relay': True},
    {'id': '44f91d91', 'bot': 'Desk', 'status': 'completed', 'source': 'f21ea9d7', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'f21ea9d7', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True, 'reported': ['target']},
    {'id': 'b6326933', 'bot': 'Desk', 'status': 'completed', 'source': 'f21ea9d7', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'f21ea9d7', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True, 'reported': ['target']},
    {'id': '65e24636', 'bot': 'Gumroad Dev', 'status': 'completed', 'source': 'b6326933', 'requester': '34d9ba7b', 'execution': '65e24636', 'relay': True, 'reported': ['target']},
    {'id': 'ffa6d1cd', 'bot': 'Desk', 'status': 'completed', 'source': 'f21ea9d7', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': 'f21ea9d7', 'callback': '34d9ba7b', 'execution': 'b8057ab9'},
    {'id': '6a524539', 'bot': 'Desk', 'status': 'completed', 'source': '65e24636', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': '65e24636', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True, 'reported': ['target']},
    {'id': '3910baa3', 'bot': 'Coconut Care', 'status': 'waiting', 'source': '6a524539', 'requester': '34d9ba7b', 'waiting': ['51b30b3d'], 'native': True, 'execution': '3910baa3', 'relay': True},
    {'id': '160371b3', 'bot': 'Desk', 'status': 'completed', 'source': '3910baa3', 'assignment': 'b8057ab9', 'is_return': True, 'resolved': '3910baa3', 'callback': '34d9ba7b', 'execution': 'b8057ab9', 'relay': True},
    {'id': '51b30b3d', 'bot': 'Gumroad Dev', 'status': 'working', 'source': '3910baa3', 'requester': '3910baa3', 'execution': '51b30b3d', 'relay': True},
]

# (bot, PR, creating task, review task) of the PR records that GitHub observed.
ARTIFACTS = [
    ('Desk', 'sibling', 'b8057ab9', 'b8057ab9'),
    ('Desk', 'target', 'b8057ab9', 'b8057ab9'),
    ('Gumroad Dev', 'sibling', 'a43e8fe6', 'a43e8fe6'),
    ('Gumroad Dev', 'target', 'f21ea9d7', 'f21ea9d7'),
]
