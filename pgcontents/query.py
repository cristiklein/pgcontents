"""
Database Queries for PostgresContentsManager.
"""
from textwrap import dedent

from psycopg2.errorcodes import FOREIGN_KEY_VIOLATION
from sqlalchemy import (
    and_,
    asc,
    cast,
    desc,
    func,
    null,
    select,
    text,
    Unicode,
)

from sqlalchemy.exc import IntegrityError

from .api_utils import (
    from_api_dirname,
    from_api_filename,
    split_api_filepath,
)
from .constants import UNLIMITED
from .db_utils import (
    ignore_unique_violation,
    to_dict,
)
from .error import (
    DirectoryNotEmpty,
    FileExists,
    FileTooLarge,
    NoSuchCheckpoint,
    NoSuchDirectory,
    NoSuchFile,
)
from .schema import(
    checkpoints,
    directories,
    files,
    remote_checkpoints,
    users,
)

# =====
# Users
# =====


def ensure_db_user(db, user_id):
    """
    Add a new user if they don't already exist.
    """
    with ignore_unique_violation():
        db.execute(
            users.insert().values(id=user_id),
        )


def purge_user(db, user_id):
    """
    Delete a user and all of their resources.
    """
    db.execute(files.delete().where(
        files.c.user_id == user_id
    ))
    db.execute(directories.delete().where(
        directories.c.user_id == user_id
    ))
    db.execute(users.delete().where(
        users.c.id == user_id
    ))


# ===========
# Directories
# ===========
def create_directory(db, user_id, api_path):
    """
    Create a directory.
    """
    name = from_api_dirname(api_path)
    if name == '/':
        parent_name = null()
        parent_user_id = null()
    else:
        # Convert '/foo/bar/buzz/' -> '/foo/bar/'
        parent_name = name[:name.rindex('/', 0, -1) + 1]
        parent_user_id = user_id

    db.execute(
        directories.insert().values(
            name=name,
            user_id=user_id,
            parent_name=parent_name,
            parent_user_id=parent_user_id,
        )
    )


def ensure_directory(db, user_id, api_path):
    """
    Ensure that the given user has the given directory.
    """
    with ignore_unique_violation():
        create_directory(db, user_id, api_path)


def _is_in_directory(table, user_id, db_dirname):
    """
    Return a WHERE clause that matches entries in a directory.

    Parameterized on table because this clause is re-used between files and
    directories.
    """
    return and_(
        table.c.parent_name == db_dirname,
        table.c.user_id == user_id,
    )


def _directory_default_fields():
    """
    Default fields returned by a directory query.
    """
    return [
        directories.c.name,
    ]


def delete_directory(db, user_id, api_path):
    """
    Delete a directory.

    TODO: Consider making this a soft delete.
    """
    db_dirname = from_api_dirname(api_path)
    try:
        result = db.execute(
            directories.delete().where(
                and_(
                    directories.c.user_id == user_id,
                    directories.c.name == db_dirname,
                )
            )
        )
    except IntegrityError as error:
        if error.orig.pgcode != FOREIGN_KEY_VIOLATION:
            raise
        raise DirectoryNotEmpty(api_path)

    rowcount = result.rowcount
    if not rowcount:
        raise NoSuchDirectory(api_path)

    return rowcount


def dir_exists(db, user_id, api_dirname):
    """
    Check if a directory exists.
    """
    return _dir_exists(db, user_id, from_api_dirname(api_dirname))


def _dir_exists(db, user_id, db_dirname):
    """
    Internal implementation of dir_exists.

    Expects a db-style path name.
    """
    return db.execute(
        select(
            [func.count(directories.c.name)],
        ).where(
            and_(
                directories.c.user_id == user_id,
                directories.c.name == db_dirname,
            ),
        )
    ).scalar() != 0


def files_in_directory(db, user_id, db_dirname):
    """
    Return files in a directory.
    """
    fields = _file_default_fields()
    rows = db.execute(
        select(
            fields,
        ).where(
            _is_in_directory(files, user_id, db_dirname),
        ).order_by(
            files.c.user_id,
            files.c.parent_name,
            files.c.name,
            files.c.created_at,
        ).distinct(
            files.c.user_id, files.c.parent_name, files.c.name,
        )
    )
    return [to_dict(fields, row) for row in rows]


def directories_in_directory(db, user_id, db_dirname):
    """
    Return subdirectories of a directory.
    """
    fields = _directory_default_fields()
    rows = db.execute(
        select(
            fields,
        ).where(
            _is_in_directory(directories, user_id, db_dirname),
        )
    )
    return [to_dict(fields, row) for row in rows]


def get_directory(db, user_id, api_dirname, content):
    """
    Return the names of all files/directories that are direct children of
    api_dirname.

    If content is False, return a bare model containing just a database-style
    name.
    """
    db_dirname = from_api_dirname(api_dirname)
    if not _dir_exists(db, user_id, db_dirname):
        raise NoSuchDirectory(api_dirname)
    if content:
        files = files_in_directory(
            db,
            user_id,
            db_dirname,
        )
        subdirectories = directories_in_directory(
            db,
            user_id,
            db_dirname,
        )
    else:
        files, subdirectories = None, None

    # TODO: Consider using namedtuples for these return values.
    return {
        'name': db_dirname,
        'files': files,
        'subdirs': subdirectories,
    }


# =====
# Files
# =====
def _file_where(user_id, api_path):
    """
    Return a WHERE clause matching the given API path and user_id.
    """
    directory, name = split_api_filepath(api_path)
    return and_(
        files.c.name == name,
        files.c.user_id == user_id,
        files.c.parent_name == directory,
    )


def _file_creation_order():
    """
    Return an order_by on file creation date.
    """
    return desc(files.c.created_at)


def _select_file(user_id, api_path, fields, limit):
    """
    Return a SELECT statement that returns the latest N versions of a file.
    """
    query = select(fields).where(
        _file_where(user_id, api_path),
    ).order_by(
        _file_creation_order(),
    )
    if limit is not None:
        query = query.limit(limit)

    return query


def _file_default_fields():
    """
    Default fields returned by a file query.
    """
    return [
        files.c.name,
        files.c.created_at,
        files.c.parent_name,
    ]


def get_file(db, user_id, api_path, include_content):
    """
    Get file data for the given user_id and path.

    Include content only if include_content=True.
    """
    query_fields = _file_default_fields()
    if include_content:
        query_fields.append(files.c.content)

    result = db.execute(
        _select_file(user_id, api_path, query_fields, limit=1),
    ).first()

    if result is None:
        raise NoSuchFile(api_path)
    return to_dict(query_fields, result)


def delete_file(db, user_id, api_path):
    """
    Delete a file.

    TODO: Consider making this a soft delete.
    """
    directory, name = split_api_filepath(api_path)
    result = db.execute(
        files.delete().where(
            _file_where(user_id, api_path)
        )
    )

    rowcount = result.rowcount
    if not rowcount:
        raise NoSuchFile(api_path)

    # TODO: This is misleading because we allow multiple files with the same
    # user_id/name as checkpoints.  Consider de-duping this in some way?
    return rowcount


def file_exists(db, user_id, path):
    """
    Check if a file exists.
    """
    try:
        get_file(db, user_id, path, include_content=False)
        return True
    except NoSuchFile:
        return False


def rename_file(db, user_id, old_api_path, new_api_path):
    """
    Rename a file. The file must stay in the same directory.

    TODO: Consider allowing renames to existing directories.
    TODO: Don't do anything if paths are the same.
    """
    old_dir, old_name = split_api_filepath(old_api_path)
    new_dir, new_name = split_api_filepath(new_api_path)
    if old_dir != new_dir:
        raise ValueError(
            dedent(
                """
                Can't rename file to new directory.
                Old Path: {old_api_path}
                New Path: {new_api_path}
                """.format(
                    old_api_path=old_api_path,
                    new_api_path=new_api_path
                )
            )
        )

    if file_exists(db, user_id, new_api_path):
        raise FileExists(new_api_path)

    db.execute(
        files.update().where(
            (files.c.user_id == user_id)
            & (files.c.parent_name == new_dir)
        ).values(
            name=new_name,
        )
    )


def check_content(content, max_size_bytes):
    """
    Check that the content to be saved isn't too large to store.
    """
    if max_size_bytes != UNLIMITED and len(content) > max_size_bytes:
        raise FileTooLarge()


def save_file(db, user_id, path, content, max_size_bytes):
    """
    Save a file.
    """
    check_content(content, max_size_bytes)
    directory, name = split_api_filepath(path)
    res = db.execute(
        files.insert().values(
            name=name,
            user_id=user_id,
            parent_name=directory,
            content=content,
        )
    )
    return res


# ===========
# Checkpoints
# ===========
def _checkpoint_default_fields():
    return checkpoints.c.id, checkpoints.c.created_at


def _get_checkpoints(db, user_id, api_path, limit=None):
    """
    Get checkpoints from the database.
    """
    raise NotImplementedError()
    query_fields = _checkpoint_default_fields()
    query = select(
        query_fields,
    ).where(
        _file_where(user_id, api_path),
    ).order_by(
        desc(files.c.created_at)
    )
    if limit is not None:
        query = query.limit(limit)

    results = [to_dict(query_fields, record) for record in db.execute(query)]
    if not results:
        raise NoSuchFile(api_path)

    return results


def create_checkpoint(db, user_id, api_path):
    """
    Create a checkpoint.
    """
    latest_version = _select_file(
        user_id,
        api_path,
        [files.c.id],
        limit=1,
    )

    return_fields = _checkpoint_default_fields()
    query = checkpoints.insert().values(
        file_id=latest_version,
    ).returning(
        *return_fields
    )
    results = [to_dict(return_fields, record) for record in db.execute(query)]
    if not results:
        raise NoSuchFile(api_path)
    assert len(results) == 1
    return results[0]


def list_checkpoints(db, user_id, api_path):
    """
    Get all checkpoints for an api_path.
    """
    query_fields = _checkpoint_default_fields()
    query = _select_file(
        user_id,
        api_path,
        query_fields,
        limit=None,
    ).select_from(
        files.join(
            checkpoints,
            files.c.id == checkpoints.c.file_id,
        )
    )
    results = [to_dict(query_fields, record) for record in db.execute(query)]
    return results


def restore_checkpoint(db, user_id, api_path, checkpoint_id):
    """
    Restore a checkpoint by bumping its file's created_at date to now.
    """
    query = files.update().where(
        checkpoints.c.id == checkpoint_id
    ).where(
        files.c.id == checkpoints.c.file_id,
    ).where(
        _file_where(user_id, api_path),
    ).values(
        created_at=func.now(),
    )
    result = db.execute(
        query,
    )
    if not result.rowcount:
        raise NoSuchFile()


def delete_checkpoint(db, user_id, api_path, checkpoint_id):
    """
    Delete a checkpoint.
    """
    # We do this manually because SQLAlchemy doesn't support DELETE FROM USING.
    # See https://bitbucket.org/zzzeek/sqlalchemy/issue/959.
    querytext = text(
        """
        DELETE FROM checkpoints
        USING files
        WHERE
            (files.user_id = :user_id) AND
            (files.name = :name) AND
            (files.parent_name = :parent_name) AND
            (checkpoints.id = :checkpoint_id) AND
            (checkpoints.file_id = files.id)
        """
    )
    directory, name = split_api_filepath(api_path)
    result = db.execute(
        querytext,
        user_id=user_id,
        name=name,
        parent_name=directory,
        checkpoint_id=checkpoint_id,
    )

    if not result.rowcount:
        raise NoSuchFile()


# =======================================
# Checkpoints (PostgresCheckpoints)
# =======================================
def _remote_checkpoint_default_fields():
    return [
        cast(remote_checkpoints.c.id, Unicode),
        remote_checkpoints.c.last_modified,
    ]


def delete_single_remote_checkpoint(db, user_id, api_path, checkpoint_id):
    db_path = from_api_filename(api_path)
    result = db.execute(
        remote_checkpoints.delete().where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == db_path,
                remote_checkpoints.c.id == int(checkpoint_id),
            ),
        ),
    )

    if not result.rowcount:
        raise NoSuchCheckpoint(api_path, checkpoint_id)


def delete_remote_checkpoints(db, user_id, api_path):
    db_path = from_api_filename(api_path)
    db.execute(
        remote_checkpoints.delete().where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == db_path,
            ),
        )
    )


def list_remote_checkpoints(db, user_id, api_path):
    db_path = from_api_filename(api_path)
    fields = _remote_checkpoint_default_fields()
    results = db.execute(
        select(fields).where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == db_path,
            ),
        ).order_by(
            asc(remote_checkpoints.c.last_modified),
        ),
    )

    return [to_dict(fields, row) for row in results]


def move_single_remote_checkpoint(db,
                                  user_id,
                                  src_api_path,
                                  dest_api_path,
                                  checkpoint_id):
    src_db_path = from_api_filename(src_api_path)
    dest_db_path = from_api_filename(dest_api_path)
    result = db.execute(
        remote_checkpoints.update().where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == src_db_path,
                remote_checkpoints.c.id == int(checkpoint_id),
            ),
        ).values(
            path=dest_db_path,
        ),
    )

    if not result.rowcount:
        raise NoSuchCheckpoint(src_api_path, checkpoint_id)


def move_remote_checkpoints(db, user_id, src_api_path, dest_api_path):
    src_db_path = from_api_filename(src_api_path)
    dest_db_path = from_api_filename(dest_api_path)
    db.execute(
        remote_checkpoints.update().where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == src_db_path,
            ),
        ).values(
            path=dest_db_path,
        ),
    )


def get_remote_checkpoint(db, user_id, api_path, checkpoint_id):
    db_path = from_api_filename(api_path)
    fields = [remote_checkpoints.c.content]
    result = db.execute(
        select(
            fields,
        ).where(
            and_(
                remote_checkpoints.c.user_id == user_id,
                remote_checkpoints.c.path == db_path,
                remote_checkpoints.c.id == int(checkpoint_id),
            ),
        )
    ).first()

    if result is None:
        raise NoSuchCheckpoint(api_path, checkpoint_id)

    return to_dict(fields, result)


def save_remote_checkpoint(db, user_id, api_path, content):
    return_fields = _remote_checkpoint_default_fields()
    result = db.execute(
        remote_checkpoints.insert().values(
            user_id=user_id,
            path=from_api_filename(api_path),
            content=content,
        ).returning(
            *return_fields
        ),
    ).first()

    return to_dict(return_fields, result)


def purge_remote_checkpoints(db, user_id):
    """
    Delete all database records for the given user_id.
    """
    db.execute(
        remote_checkpoints.delete().where(
            remote_checkpoints.c.user_id == user_id,
        )
    )


def latest_remote_checkpoints(db, user_id):
    """
    Get the latest version of each file for the user.
    """
    query_fields = [
        remote_checkpoints.c.id,
        remote_checkpoints.c.path,
    ]

    query = select(query_fields).where(
        remote_checkpoints.c.user_id == user_id,
    ).order_by(
        remote_checkpoints.c.path,
        desc(remote_checkpoints.c.last_modified),
    ).distinct(
        remote_checkpoints.c.path,
    )
    results = db.execute(query)
    return (to_dict(query_fields, row) for row in results)