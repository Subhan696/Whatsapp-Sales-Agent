/**
 * DB-backed Baileys auth state — one row per (tenant_id, key_name) in
 * baileys_auth_state, instead of one file per key on local disk. Lets
 * sessions survive a redeploy onto a different machine, not just a
 * same-box restart.
 *
 * Mirrors Baileys' own useMultiFileAuthState (see
 * node_modules/@whiskeysockets/baileys/lib/Utils/use-multi-file-auth-state.js)
 * line for line — same BufferJSON replacer/reviver, same
 * app-state-sync-key special case, same per-key mutex pattern — just
 * swapping fs.readFile/writeFile for a Postgres SELECT/UPSERT.
 */
const { Mutex } = require('async-mutex');
const { initAuthCreds, BufferJSON, proto } = require('@whiskeysockets/baileys');

const rowLocks = new Map();

function getRowLock(lockKey) {
  let mutex = rowLocks.get(lockKey);
  if (!mutex) {
    mutex = new Mutex();
    rowLocks.set(lockKey, mutex);
  }
  return mutex;
}

async function usePostgresAuthState(pool, tenantId) {
  const writeData = async (data, keyName) => {
    const lockKey = `${tenantId}:${keyName}`;
    const release = await getRowLock(lockKey).acquire();
    try {
      const json = JSON.stringify(data, BufferJSON.replacer);
      await pool.query(
        `INSERT INTO baileys_auth_state (tenant_id, key_name, value, updated_at)
         VALUES ($1, $2, $3, now())
         ON CONFLICT (tenant_id, key_name)
         DO UPDATE SET value = EXCLUDED.value, updated_at = now()`,
        [tenantId, keyName, json]
      );
    } finally {
      release();
    }
  };

  const readData = async (keyName) => {
    const lockKey = `${tenantId}:${keyName}`;
    const release = await getRowLock(lockKey).acquire();
    try {
      const res = await pool.query(
        'SELECT value FROM baileys_auth_state WHERE tenant_id = $1 AND key_name = $2',
        [tenantId, keyName]
      );
      if (res.rows.length === 0) return null;
      return JSON.parse(res.rows[0].value, BufferJSON.reviver);
    } catch {
      return null;
    } finally {
      release();
    }
  };

  const removeData = async (keyName) => {
    const lockKey = `${tenantId}:${keyName}`;
    const release = await getRowLock(lockKey).acquire();
    try {
      await pool.query('DELETE FROM baileys_auth_state WHERE tenant_id = $1 AND key_name = $2', [tenantId, keyName]);
    } finally {
      release();
    }
  };

  const creds = (await readData('creds')) || initAuthCreds();

  return {
    state: {
      creds,
      keys: {
        get: async (type, ids) => {
          const data = {};
          await Promise.all(
            ids.map(async (id) => {
              let value = await readData(`${type}-${id}`);
              if (type === 'app-state-sync-key' && value) {
                value = proto.Message.AppStateSyncKeyData.fromObject(value);
              }
              data[id] = value;
            })
          );
          return data;
        },
        set: async (data) => {
          const tasks = [];
          for (const category in data) {
            for (const id in data[category]) {
              const value = data[category][id];
              const keyName = `${category}-${id}`;
              tasks.push(value ? writeData(value, keyName) : removeData(keyName));
            }
          }
          await Promise.all(tasks);
        },
      },
    },
    saveCreds: () => writeData(creds, 'creds'),
  };
}

async function clearAuthState(pool, tenantId) {
  await pool.query('DELETE FROM baileys_auth_state WHERE tenant_id = $1', [tenantId]);
}

module.exports = { usePostgresAuthState, clearAuthState };
