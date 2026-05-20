# Template `one-on-one`

**Estado**: 📝 Design doc — implementación en construcción. Ver [el patrón groups/](../groups) mientras tanto.

Agente WhatsApp para **conversaciones 1:1** con contactos: clientes, leads, equipo. Inspirado en el patrón "Iris" — una asistente WA del owner que responde mensajes directos, deriva al owner cuando hace falta, y aprende de cada interacción.

## Diferencias clave vs `groups/`

| Aspecto | `groups/` | `one-on-one/` |
|---|---|---|
| Unidad de sesión | grupo (`@g.us`) | contacto (`@s.whatsapp.net`) |
| SOUL | uno por grupo (versionado) | global + por `contact_kind` |
| Mention detection | sí (LIDs, @phoenix, @JIDnumber) | N/A (todo DM es para el agente) |
| Tickets | no (discusión launcher) | sí (handoff al owner) |
| KBs | suscritas a grupos | suscritas globalmente o por `contact_kind` |
| Crisis detector | igual | igual |

## Modelo de datos propuesto

```
contacts
  - phone (PK)
  - display_name
  - kind (owner | regular | prospect | colega | otro)
  - notes
  - first_seen
  - last_seen

threads
  - contact_id (FK)
  - status (open | closed)
  - opened_at / closed_at

messages
  - thread_id (FK)
  - direction (in | out)
  - body / media_hint
  - ts
  - model_used / tokens_in / tokens_out
  - intent (lookup_fact | open_ticket | ...)

tickets
  - thread_id (FK)
  - kind (consulta | cotizacion | opinion | urgencia | otro)
  - summary
  - status (open | awaiting_owner | awaiting_contact | closed)
  - owner_response
  - created_at / updated_at

kb / kb_facts (mismo schema que groups/, sin tabla group_kbs)
  - kb_facts puede tener un campo opcional `applies_to_kind` para restringir
    facts a tipos de contacto específicos.

audit_log (igual que groups/)
app_config (igual que groups/, mismo set de keys + opcionalmente añadir
  `default_kind` para nuevos contactos)
```

## Pipeline `/chat` propuesto

1. **Resolve contact** por `wa_jid`. Auto-register con `kind="regular"` si es nuevo.
2. **Gating** según `contact.kind`:
   - `owner` → siempre responde.
   - cualquier otro → responde según política global (default: responde a todos los DMs; o `on_command_only` si configurado).
3. **Slash commands del owner** (igual que groups/).
4. **Safety: detector de crisis** (skip si owner).
5. **LLM loop**: SOUL global + KBs aplicables + history del thread.
6. **Tools tool-use**: `lookup_kb_fact`, `list_kb_facts`, `remember_fact`, `open_ticket(kind, summary)`, `update_my_soul`, `create_kb`, `subscribe_kb_globally`.
7. **Ticket flow**:
   - Agent crea ticket → notifica owner DM con botones (resolver / responder / cerrar).
   - Owner responde → agent traduce a tono adecuado y envía al contacto.

## UI propuesta

- `/` dashboard: contactos activos, tickets abiertos, mensajes 24h.
- `/setup` pair WA (idéntico a groups/).
- `/contacts` listado paginado, filtros por kind, búsqueda.
- `/contacts/{phone}` ficha: thread + tickets + edición de kind y notas.
- `/tickets` kanban por status.
- `/kbs`, `/kbs/{slug}` (igual que groups/).
- `/audit` (igual).
- `/settings` (igual + `default_kind`).

## Plan de implementación

Pull request o issue bienvenidos. La forma más rápida hoy:

1. Clona este repo.
2. Copia `groups/` a `my-agent/`.
3. Refactor:
   - Schema: reemplazar `Group`+`GroupSoul`+`GroupKb` por `Contact`+`Ticket`. Mantener `KnowledgeBase`+`KbFact`+`AuditLog`+`AppConfig`+`Message`.
   - `chat.py`: cambiar resolución de contexto de grupo a contacto. Gating por `contact.kind`.
   - `tools.py`: agregar `open_ticket`. Quitar tools de grupo (`update_group_soul`, etc.) o cambiar su scope.
   - `wa-listener`: quitar filtro `inGroup` — aceptar todos los DMs.
   - UI: reemplazar vistas de grupos por vistas de contactos + tickets.

Si vas por este camino y quieres contribuir el resultado de vuelta, abre un PR contra `one-on-one/`.

## Por qué este template aún no está completo

El autor está priorizando el `groups/` (en producción real) sobre la abstracción del 1:1. Cuando el original del que se deriva (Iris) tenga más uso/madurez, se publica aquí.
