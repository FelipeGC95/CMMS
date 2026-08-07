# Guía para colaborar en el repositorio CMMS

Instructivo para una persona **invitada** al repositorio `FelipeGC95/CMMS` que quiere
clonarlo en su equipo y trabajar en conjunto.

---

## 1. Requisitos previos

Antes de empezar, instala en tu equipo:

- **Git** → https://git-scm.com/downloads
  (En Windows incluye *Git Credential Manager*, que gestiona el inicio de sesión).
- Una cuenta de **GitHub**.
- (Opcional) Un editor como **VS Code** o **PyCharm**.

Verifica que Git esté instalado:

```bash
git --version
```

---

## 2. Aceptar la invitación

1. El dueño del repo (FelipeGC95) debe invitarte desde
   **GitHub → repo CMMS → Settings → Collaborators → Add people**.
2. Recibirás un correo o una notificación con el enlace de invitación.
3. Ábrelo y pulsa **Accept invitation**.

Sin aceptar la invitación no podrás clonar (es un repo privado).

---

## 3. Configurar tu identidad de Git (una sola vez)

Para que tus commits queden a tu nombre:

```bash
git config --global user.name "Tu Nombre"
git config --global user.email "tu-correo@ejemplo.com"
```

> **Si usas más de una cuenta de GitHub en el mismo equipo**, no pongas la identidad
> global; configúrala solo dentro del repo (ver paso 4, después de clonar):
>
> ```bash
> git config user.name "Tu Nombre"
> git config user.email "tu-correo@ejemplo.com"
> ```

---

## 4. Clonar el repositorio

Ubícate en la carpeta donde quieras guardar el proyecto y clona:

```bash
git clone https://github.com/FelipeGC95/CMMS.git
cd CMMS
```

La primera vez, Git Credential Manager abrirá una ventana para que inicies sesión
en GitHub con **tu** cuenta invitada. Autentícate ahí.

> **¿Tienes varias cuentas de GitHub en el equipo?** Clona indicando tu usuario en la
> URL para que las credenciales no se mezclen:
>
> ```bash
> git clone https://TU_USUARIO@github.com/FelipeGC95/CMMS.git
> ```

---

## 5. Flujo de trabajo recomendado (ramas)

Para trabajar en conjunto sin pisarse, **no trabajes directo en `main`**. Crea una
rama por cada tarea:

```bash
# 1. Asegúrate de partir desde la última versión de main
git checkout main
git pull origin main

# 2. Crea tu rama de trabajo
git checkout -b feature/nombre-de-tu-tarea

# 3. Trabaja y guarda cambios
git add .
git commit -m "Descripción clara del cambio"

# 4. Sube tu rama a GitHub
git push -u origin feature/nombre-de-tu-tarea
```

Luego, en GitHub, abre un **Pull Request (PR)** de tu rama hacia `main` para que se
revise y se fusione.

---

## 6. Mantenerte actualizado

Antes de empezar a trabajar cada día, trae los últimos cambios de tus compañeros:

```bash
git checkout main
git pull origin main
```

Si estás en tu rama y quieres incorporar lo nuevo de `main`:

```bash
git checkout feature/nombre-de-tu-tarea
git merge main
```

---

## 7. Comandos útiles del día a día

| Acción                          | Comando                          |
|---------------------------------|----------------------------------|
| Ver estado de los archivos      | `git status`                     |
| Ver ramas                       | `git branch -a`                  |
| Cambiar de rama                 | `git checkout nombre-rama`       |
| Ver historial de commits        | `git log --oneline --graph`      |
| Descartar cambios de un archivo | `git checkout -- archivo`        |
| Traer cambios del remoto        | `git pull`                       |
| Subir tus cambios               | `git push`                       |

---

## 8. Resolución de conflictos (rápido)

Si al hacer `pull` o `merge` aparece un conflicto:

1. Git marcará los archivos en conflicto (`git status` los lista).
2. Abre cada archivo y busca los bloques:
   ```
   <<<<<<< HEAD
   tu versión
   =======
   la versión del compañero
   >>>>>>> main
   ```
3. Edita para dejar la versión final correcta y borra las marcas `<<<`, `===`, `>>>`.
4. Marca el conflicto como resuelto y confirma:
   ```bash
   git add archivo-en-conflicto
   git commit
   ```

---

## Reglas de convivencia sugeridas

- Haz commits pequeños y con mensajes descriptivos.
- Nunca subas contraseñas, tokens ni archivos secretos (usa `.gitignore`).
- Antes de subir, ejecuta un `git pull` para evitar conflictos innecesarios.
- Usa Pull Requests para revisar el código entre todos antes de fusionar a `main`.

 https://www.youtube.com/watch?v=9oVfhupb77w
