from __future__ import print_function
from flask.ext.script import Manager
from flask import current_app, jsonify
from datetime import datetime
from time import gmtime, strftime
from util import create_path, remove_old_archives, get_columns, slugify, DumpJSONEncoder
import subprocess
import tarfile
import shutil
import errno
import sys
import os

from critiquebrainz.data import db, explode_db_url
from critiquebrainz.data import model
from critiquebrainz.data.model.review import Review
from critiquebrainz.data.model.license import License

backup_manager = Manager()


@backup_manager.command
def dump_db(location=os.path.join(os.getcwd(), 'backup'), rotate=False):
    """Create complete dump of PostgreSQL database.

    This command creates database dump using pg_dump and puts it into specified directory
    (default is *backup*). It's also possible to remove all previously created backups
    except two most recent ones. If you want to do that, set *rotate* argument to True.

    File with a dump will be a tar archive with a timestamp in the name: `%Y%m%d-%H%M%S.tar.bz2`.
    """

    # Creating backup directory, if needed
    create_path(location)

    FILE_PREFIX = "cb-backup-"
    db_hostname, db_name, db_username, db_password = explode_db_url(current_app.config['SQLALCHEMY_DATABASE_URI'])

    print('Creating database dump in "%s"...' % location)

    # Executing pg_dump command
    # More info about it is available at http://www.postgresql.org/docs/9.3/static/app-pgdump.html
    dump_file = "%s/%s%s" % (location, FILE_PREFIX, strftime("%Y%m%d-%H%M%S", gmtime()))
    if subprocess.call('pg_dump -Ft "%s" > "%s.tar"' % (db_name, dump_file), shell=True) != 0:
        raise Exception("Failed to create database dump!")

    # Compressing created dump
    if subprocess.call('bzip2 "%s.tar"' % dump_file, shell=True) != 0:
        raise Exception("Failed to create database dump!")

    print('Created %s.tar.bz2' % dump_file)

    if rotate:
        print("Removing old backups (except two latest)...")
        remove_old_archives(location, "%s[0-9]+-[0-9]+.tar" % FILE_PREFIX,
                            is_dir=False, sort_key=lambda x: os.path.getmtime(x))

    print("Done!")


@backup_manager.command
def dump_json(location=os.path.join(os.getcwd(), 'dump'), rotate=False):
    """Create JSON dumps with all reviews.

    This command will create an archive for each license available on CB.
    Archives will be put into a specified directory (default is *dump*).
    """
    current_app.json_encoder = DumpJSONEncoder
    temp_dir = '%s/temp' % location
    create_path(temp_dir)

    print("Creating new archives...")
    for license in License.query.all():
        safe_name = slugify(license.id)
        with tarfile.open("%s/critiquebrainz-%s-%s-json.tar.bz2" % (location, datetime.today().strftime('%Y%m%d'), safe_name), "w:bz2") as tar:
            license_dir = '%s/%s' % (temp_dir, safe_name)
            create_path(license_dir)

            # Finding release groups that have reviews with current license
            query = db.session.query(Review.release_group).group_by(Review.release_group)
            for release_group in query.all():
                release_group = release_group[0]
                # Creating directory structure for release group and dumping reviews
                rg_dir_part = '%s/%s' % (release_group[0:1], release_group[0:2])
                reviews = Review.list(release_group, license_id=license.id)[0]
                if len(reviews) > 0:
                    rg_dir = '%s/%s' % (license_dir, rg_dir_part)
                    create_path(rg_dir)
                    f = open('%s/%s.json' % (rg_dir, release_group), 'w+')
                    f.write(jsonify(reviews=[r.to_dict() for r in reviews]).data)
                    f.close()

            tar.add(license_dir, arcname='reviews')

            # Copying legal text
            tar.add("critiquebrainz/data/licenses/%s.txt" % safe_name, arcname='COPYING')

            print(" + %s/critiquebrainz-%s-%s-json.tar.bz2" % (location, datetime.today().strftime('%Y%m%d'), safe_name))

    shutil.rmtree(temp_dir)  # Cleanup

    if rotate:
        print("Removing old sets of archives (except two latest)...")
        remove_old_archives(location, "critiquebrainz-[0-9]+-[-\w]+-json.tar.bz2",
                            is_dir=False, sort_key=lambda x: os.path.getmtime(x))

    print("Done!")


@backup_manager.command
def export(location=os.path.join(os.getcwd(), 'export'), rotate=False):
    """Creates a set of archives with public data.

    1. Base archive with license-independent data (users, licenses).
    2. Archive with all reviews and revisions.
    3... Separate archives for each license (contain reviews and revisions associated with specific license).
    """
    print("Creating new archives...")
    time_now = datetime.today()

    # Getting psycopg2 cursor
    cursor = db.session.connection().connection.cursor()

    # Creating a directory where all dumps will go
    dump_dir = '%s/%s' % (location, time_now.strftime('%Y%m%d-%H%M%S'))
    temp_dir = '%s/temp' % dump_dir
    create_path(temp_dir)

    # Preparing meta files
    with open('%s/TIMESTAMP' % temp_dir, 'w') as f:
        f.write(time_now.isoformat(' '))
    with open('%s/SCHEMA_SEQUENCE' % temp_dir, 'w') as f:
        f.write(str(model.__version__))

    # BASE ARCHIVE
    # Archiving stuff that is independent from licenses (users, licenses)
    with tarfile.open("%s/cbdump.tar.bz2" % dump_dir, "w:bz2") as tar:
        base_archive_dir = '%s/cbdump' % temp_dir
        create_path(base_archive_dir)

        # Dumping tables
        base_archive_tables_dir = '%s/cbdump' % base_archive_dir
        create_path(base_archive_tables_dir)
        with open('%s/user_sanitised' % base_archive_tables_dir, 'w') as f:
             cursor.copy_to(f, '"user"', columns=('id', 'created',  'display_name', 'musicbrainz_id'))
        with open('%s/license' % base_archive_tables_dir, 'w') as f:
            cursor.copy_to(f, 'license', columns=get_columns(model.License))
        tar.add(base_archive_tables_dir, arcname='cbdump')

        # Including additional information about this archive
        # Copying the most restrictive license there (CC BY-NC-SA 3.0)
        tar.add('critiquebrainz/data/licenses/cc-by-nc-sa-30.txt', arcname='COPYING')
        tar.add('%s/TIMESTAMP' % temp_dir, arcname='TIMESTAMP')
        tar.add('%s/SCHEMA_SEQUENCE' % temp_dir, arcname='SCHEMA_SEQUENCE')

        print(" + %s/cbdump.tar.bz2" % dump_dir)

    # REVIEWS
    # Archiving review tables (review, revision)

    # 1. COMBINED
    # Archiving all reviews (any license)
    with tarfile.open("%s/cbdump-reviews-all.tar.bz2" % dump_dir, "w:bz2") as tar:
        # Dumping tables
        reviews_combined_tables_dir = '%s/cbdump-reviews-all' % temp_dir
        create_path(reviews_combined_tables_dir)
        with open('%s/review' % reviews_combined_tables_dir, 'w') as f:
            cursor.copy_to(f, 'review', columns=get_columns(model.Review))
        with open('%s/revision' % reviews_combined_tables_dir, 'w') as f:
            cursor.copy_to(f, 'revision', columns=get_columns(model.Revision))
        tar.add(reviews_combined_tables_dir, arcname='cbdump')

        # Including additional information about this archive
        # Copying the most restrictive license there (CC BY-NC-SA 3.0)
        tar.add('critiquebrainz/data/licenses/cc-by-nc-sa-30.txt', arcname='COPYING')
        tar.add('%s/TIMESTAMP' % temp_dir, arcname='TIMESTAMP')
        tar.add('%s/SCHEMA_SEQUENCE' % temp_dir, arcname='SCHEMA_SEQUENCE')

        print(" + %s/cbdump-reviews-all.tar.bz2" % dump_dir)

    # 2. SEPARATE
    # Creating separate archives for each license
    for license in License.query.all():
        safe_name = slugify(license.id)
        with tarfile.open("%s/cbdump-reviews-%s.tar.bz2" % (dump_dir, safe_name), "w:bz2") as tar:
            # Dumping tables
            tables_dir = '%s/%s' % (temp_dir, safe_name)
            create_path(tables_dir)
            with open('%s/review' % tables_dir, 'w') as f:
                cursor.copy_to(f, "(SELECT (%s) FROM review WHERE license_id = '%s')" %
                               (', '.join(get_columns(model.Review)), license.id))
            with open('%s/revision' % tables_dir, 'w') as f:
                cursor.copy_to(f, "(SELECT (revision.%s) FROM revision JOIN review ON revision.review_id = review.id WHERE review.license_id = '%s')" %
                               (', revision.'.join(get_columns(model.Revision)), license.id))
            tar.add(tables_dir, arcname='cbdump')

            # Including additional information about this archive
            tar.add('critiquebrainz/data/licenses/%s.txt' % safe_name, arcname='COPYING')
            tar.add('%s/TIMESTAMP' % temp_dir, arcname='TIMESTAMP')
            tar.add('%s/SCHEMA_SEQUENCE' % temp_dir, arcname='SCHEMA_SEQUENCE')

            print(" + %s/cbdump-reviews-%s.tar.bz2" % (dump_dir, safe_name))

    shutil.rmtree(temp_dir)  # Cleanup

    if rotate:
        print("Removing old dumps (except two latest)...")
        remove_old_archives(location, "[0-9]+-[0-9]+", is_dir=True)

    print("Done!")


@backup_manager.command
def importer(archive, temp_dir="temp"):
    """Imports database dump (archive) produced by export command.

    Before importing make sure that all required data is already imported or exists in the archive. For example,
    importing will fail if you'll try to import review without users or licenses. Same applies to revisions. To get more
    information about various dependencies see database schema.

    You should only import data into empty tables. Data will not be imported into tables that already have rows. This is
    done to prevent conflicts. Feel free improve current implementation. :)

    Importing only supported for bzip2 compressed tar archives. It will fail if version of the schema that provided
    archive requires is different from the current. Make sure you have the latest dump available.
    """
    archive = tarfile.open(archive, 'r:bz2')
    archive.extractall(temp_dir)

    # Verifying schema version
    try:
        with open('%s/SCHEMA_SEQUENCE' % temp_dir) as f:
            archive_version = f.readline()
            if archive_version != str(model.__version__):
                sys.exit("Incorrect schema version! Expected: %d, got: %c. Please, get the latest version of the dump."
                         % (model.__version__, archive_version))
    except IOError as exception:
        if exception.errno == errno.ENOENT:
            print("Can't find SCHEMA_SEQUENCE in the specified archive. Importing might fail.")
        else:
            sys.exit("Failed to open SCHEMA_SEQUENCE file. Error: %s" % exception)

    # Importing data
    import_data('%s/cbdump/user_sanitised' % temp_dir, model.User, ('id', 'created',  'display_name', 'musicbrainz_id'))
    import_data('%s/cbdump/license' % temp_dir, model.License)
    import_data('%s/cbdump/review' % temp_dir, model.Review)
    import_data('%s/cbdump/revision' % temp_dir, model.Revision)
    shutil.rmtree(temp_dir)  # Cleanup
    print("Done!")


def import_data(file_name, model, columns=None):
    db_connection = db.session.connection().connection
    cursor = db_connection.cursor()
    try:
        with open(file_name) as f:
            # Checking if table already contains any data
            if model.query.count() > 0:
                print("Table %s already contains data. Skipping." % model.__tablename__)
                return
            # and if it doesn't, trying to import data
            print("Importing data into %s table." % model.__tablename__)
            if columns is None:
                columns = get_columns(model)
            cursor.copy_from(f, '"%s"' % model.__tablename__, columns=columns)
            db_connection.commit()
    except IOError as exception:
        if exception.errno == errno.ENOENT:
            print("Can't find data file for %s table. Skipping." % model.__tablename__)
        else:
            sys.exit("Failed to open data file. Error: %s" % exception)

