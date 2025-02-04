import time
import copy
from collections import defaultdict
import geopandas as gpd
import numpy as np
import pandas
from tqdm.auto import tqdm
from multiprocessing import Process, Queue
import queue
from pyproj import Geod
from shapely.geometry import Polygon, MultiPolygon, LineString, box, MultiLineString, Point
from shapely.ops import unary_union, nearest_points
from shapely.validation import make_valid
from shapely import speedups

if speedups.available:
    speedups.enable()


def tasks_generation(grid_gdf, list_real_start_points_coords, max_distance_per_task_meter):
    closest_polygons = search_closest_polygon_to_start_points(list_real_start_points_coords, grid_gdf)

    dict_tasks = defaultdict(dict)
    for idx, geoserie in grid_gdf.iterrows():
        num_polys = len(list(geoserie.geometry.geoms))

        dict_tasks[geoserie.tiles_group_identifier]['num_polys'] = num_polys

        dict_tasks[geoserie.tiles_group_identifier]['sensor_line_length_meter'] = geoserie.sensor_line_length_meter

        # * 4 (and not * 2) cause its the big STC tiles and not the small subcell tiles for the path calculations
        # sensor_line_length_meter value inside grid_gdf is the value from settings! need to get doubled for STC
        dict_tasks[geoserie.tiles_group_identifier][
            'path_length_multipoly'] = num_polys * geoserie.sensor_line_length_meter * 4

    num_Multipolys = len(grid_gdf.iterrows())  # count the series (which are all the MultiPolygons
    tasks_list = []
    multipolys_considered = 0

    for multipoly_ident in dict_tasks:
        # go through all MultiPolygons from grid generation
        closest_startpoint_coords = ()

        for startpoint in closest_polygons:
            # go through all available startpoints and choose the one which is closest to the Multipolygon
            #  for the scanning task
            if closest_startpoint_coords.is_empty:
                closest_startpoint_coords = startpoint
            else:
                if Point(closest_startpoint_coords).distance(
                        closest_polygons[closest_startpoint_coords][multipoly_ident]) > Point(startpoint).distance(
                        closest_polygons[startpoint][multipoly_ident]):
                    closest_startpoint_coords = startpoint

        # get shortest way between Multipoly and startpoint
        # TODO insert pathfinding way inside area here later for example with func calc_path_A_to_B
        shorest_line_startpoint_multipoly = calc_path_A_to_B(Point(closest_startpoint_coords), closest_polygons[closest_startpoint_coords][multipoly_ident])

        # now look if the whole task means path from the start to the Multipoly and pack already fills max_distance_per_task
        rest = max_distance_per_task_meter - calc_length_meter(shorest_line_startpoint_multipoly) * 2 - \
               dict_tasks[multipoly_ident]['path_length_multipoly']

        # tasks_list.append()


def calc_path_A_to_B(shapely_obj1, shapely_obj2):
    # TODO Pathfinding with A* or whatever

    # path is only direct line between the 2 objects
    nearest_points_objs = nearest_points(shapely_obj1, shapely_obj2)
    line = LineString([nearest_points_objs[0], nearest_points_objs[1]])

    return line


def calc_length_meter(shapely_obj):
    """
    Calculation of the length in meter.

    :param shapely_obj: Shapely Object like LineString, MultiLineString or Polygon...

    :return: Length in meter
    """

    geod = Geod(ellps="WGS84")

    length_meter = geod.geometry_length(shapely_obj)

    return length_meter


def search_closest_polygon_to_start_points(list_start_point_coords: list, grid_gdf: gpd.GeoDataFrame):
    """
    Search the closest to the startpoint Polygon in every MultiPolygon. Check all available real Start Points.

    :param list_start_point_coords: List of start point coords (need to make Points out of it)

    :param grid_gdf: GeoDataframe with all MultiPolygons from grid generation.

    :return: Dictionary of all start point coords with tiles_group_identifier and the closest polygon.
    """
    nearest_poly_to_start_point_dict = defaultdict(dict)

    for start_point_coords in list_start_point_coords:

        for idx, geoserie in grid_gdf.iterrows():
            list_of_polys = list(geoserie.geometry.geoms)
            nearest_poly_to_start_point = min(list_of_polys, key=lambda poly: Point(start_point_coords).distance(nearest_points(Point(start_point_coords), poly)[1]))
            nearest_poly_to_start_point_dict[tuple(start_point_coords)][geoserie.tiles_group_identifier] = nearest_poly_to_start_point

    return nearest_poly_to_start_point_dict


def generate_stc_geodataframe(input_gdf: gpd.GeoDataFrame, assignment_matrix: np.ndarray, paths, tiles_group_identifier):
    print("Start dividing every polygon from grid generation into 4 subcells for usage in STC.")
    task_queue = Queue()
    done_queue = Queue()

    num_of_processes = 2  # psutil.cpu_count(logical=False)  # only physical available core count for this task

    # get big polygons from input_gdf and divide them into 4 parts for STC path planning
    # keep the old numpy_array cell positions alive in new subcells
    list_subcells_dicts = []
    task_counter = 0
    for idx, serie in enumerate(input_gdf.itertuples()):
        task_counter += 1
        column_idx = serie.column_idx  # directly use hashable entries from input_gdf
        row_idx = serie.row_idx
        assigned_startpoint = assignment_matrix[row_idx, column_idx]
        one_task = [idx, divide_polygon, (row_idx, column_idx, serie.geometry, assigned_startpoint, tiles_group_identifier)]
        task_queue.put(one_task)

    # Start worker processes
    for i in range(num_of_processes):
        Process(target=worker, args=(task_queue, done_queue)).start()

    for _ in tqdm(range(task_counter)):
        try:
            ix, list_of_dicts = done_queue.get()
            list_subcells_dicts.extend(list_of_dicts)

        except queue.Empty as e:
            print(e)

    # Tell child processes to stop
    for i in range(num_of_processes):
        task_queue.put('STOP')

    task_queue.close()
    done_queue.close()

    print("Start going through subcells and keep their relative position inside DARP numpy array.",
          "Creating LineStrings from subcell centroids.")
    measure_start = time.time()
    # create path from lines (centroid for centroid) and keep the assigned_startpoint for the geodataframe
    task_queue = Queue()
    done_queue = Queue()
    process_count = 1  # psutil.cpu_count(logical=False)  # only physical available core count for this task

    list_data_dicts = []
    task_counter = 0
    for ix_startpoint, segment in enumerate(paths):
        for line_tuples in segment:
            one_task = [task_counter, generate_linestring_data, (line_tuples, ix_startpoint, tiles_group_identifier)]
            task_queue.put(one_task)
            task_counter += 1

    # if a lot of work needs to be done then copy the original list_subcells_dicts and give every process its own
    list_of_deepcopys = [list_subcells_dicts]
    for i in range(process_count - 1):
        list_of_deepcopys.append(copy.deepcopy(list_subcells_dicts))

    # start search_worker with their own copy of list_subcells_dicts
    for i in range(process_count):
        # using search worker generates a lot of memory usage, cause of copys, so increase value with care
        Process(target=search_worker, args=(task_queue, done_queue, list_of_deepcopys[i])).start()

    for _ in tqdm(range(task_counter)):
        try:
            idx, data_dict = done_queue.get()
            if len(data_dict) > 0:
                list_data_dicts.append(data_dict)
                # append, not extend: new geoseries is just one line with assigned start point etc

        except queue.Empty as e:
            print(e)

    # Tell child processes to stop
    for i in range(process_count):
        task_queue.put('STOP')

    task_queue.close()
    done_queue.close()
    measure_end = time.time()
    print("Measured time LineString path generation: ", (measure_end - measure_start), " sec")

    gdf_subcells = gpd.GeoDataFrame(list_subcells_dicts, crs=4326).set_geometry('geometry')
    gdf_trajectory_paths = gpd.GeoDataFrame(list_data_dicts, crs=4326).set_geometry('geometry')

    gdf_collection = gpd.GeoDataFrame(pandas.concat([gdf_subcells, gdf_trajectory_paths], axis=0, ignore_index=True),
                                      crs=gdf_trajectory_paths.crs)

    # remove the row_idx, column_idx cause they are the STC MultiPolygons numpy array values and not relevant anymore
    gdf_collection.drop(['row_idx', 'column_idx'], inplace=True, axis=1)

    return gdf_collection


def generate_linestring_data(list_of_subcells_dicts, line_tuples, assigned_startpoint, tiles_group_identifier):
    # p1 row index, p1 column index, p2 row index, p2 column index = line_tuples
    p1_r_i, p1_c_i, p2_r_i, p2_c_i = line_tuples

    p1 = next((item['geometry'] for item in list_of_subcells_dicts if
               (item["column_idx"] == p1_c_i and item["row_idx"] == p1_r_i)), None)
    p2 = next((item['geometry'] for item in list_of_subcells_dicts if
               (item["column_idx"] == p2_c_i and item["row_idx"] == p2_r_i)), None)

    if p1 is not None and p2 is not None:

        data = {'tiles_group_identifier': tiles_group_identifier,
                'row_idx': np.nan,
                'column_idx': np.nan,
                'assigned_startpoint': assigned_startpoint,
                'poly': False,
                'line': True,
                'geometry': LineString([p1.centroid, p2.centroid])}

    else:
        data = {}

    return data


def divide_polygon(row_idx, column_idx, poly: Polygon, assigned_startpoint, tiles_group_identifier):
    minx, miny, maxx, maxy = poly.bounds

    l_x = (maxx - minx) / 2
    l_y = (maxy - miny) / 2

    data = [{'tiles_group_identifier': tiles_group_identifier,
             'row_idx': row_idx * 2,
             'column_idx': column_idx * 2,
             'assigned_startpoint': assigned_startpoint,
             'poly': True,
             'line': False,
             'geometry': box(minx, miny + l_y, maxx - l_x, maxy)},
            {'tiles_group_identifier': tiles_group_identifier,
             'row_idx': row_idx * 2,
             'column_idx': column_idx * 2 + 1,
             'assigned_startpoint': assigned_startpoint,
             'poly': True,
             'line': False,
             'geometry': box(minx + l_x, miny + l_y, maxx, maxy)},
            {'tiles_group_identifier': tiles_group_identifier,
             'row_idx': row_idx * 2 + 1,
             'column_idx': column_idx * 2,
             'assigned_startpoint': assigned_startpoint,
             'poly': True,
             'line': False,
             'geometry': box(minx, miny, maxx - l_x, maxy - l_y)},
            {'tiles_group_identifier': tiles_group_identifier,
             'row_idx': row_idx * 2 + 1,
             'column_idx': column_idx * 2 + 1,
             'assigned_startpoint': assigned_startpoint,
             'poly': True,
             'line': False,
             'geometry': box(minx + l_x, miny, maxx, maxy - l_y)}]

    return data


def generate_numpy_contour_array(multipoly: MultiPolygon, dict_tile_width_height):
    print("Generate numpy contour bool_area_array from STC grid MultiPolygon!")
    union_area = make_valid(unary_union(multipoly))
    minx, miny, maxx, maxy = union_area.bounds

    # scan columns from left to right
    columns_range = np.arange(minx, maxx + dict_tile_width_height['tile_width'], dict_tile_width_height['tile_width'])
    rows_range = np.arange(miny, maxy + dict_tile_width_height['tile_height'], dict_tile_width_height['tile_height'])
    # scan rows from top to bottom
    rows_range = np.flip(rows_range)
    np_bool_grid = np.full(shape=(rows_range.shape[0], columns_range.shape[0]), fill_value=False, dtype=bool)

    list_numpy_contour_positions_dicts = []

    # Create queues
    task_queue = Queue()
    done_queue = Queue()

    num_of_processes = 2  # psutil.cpu_count(logical=False)  # only physical available core count for this task
    multipoly_list = list(multipoly.geoms)
    # create tasks and push them into queue
    for idx, poly in enumerate(multipoly_list):
        one_task = [idx, check_poly_pos, (rows_range, columns_range, poly.centroid.y, poly.centroid.x,
                                          dict_tile_width_height['tile_height'],
                                          dict_tile_width_height['tile_width'])]
        task_queue.put(one_task)

    # Start worker processes
    for i in range(num_of_processes):
        Process(target=worker, args=(task_queue, done_queue)).start()

    for _ in tqdm(multipoly_list):
        try:
            ix, (row_idx, col_idx) = done_queue.get()
            np_bool_grid[row_idx, col_idx] = True

            data = {'row_idx': row_idx,
                    'column_idx': col_idx,
                    'geometry': multipoly_list[ix]}
            list_numpy_contour_positions_dicts.append(data)

        except queue.Empty as e:
            print(e)

    # Tell child processes to stop
    for i in range(num_of_processes):
        task_queue.put('STOP')

    print("Area contour numpy array (bool_area_array) generated from big STC tiles.")

    gdf_numpy_positions = gpd.GeoDataFrame(list_numpy_contour_positions_dicts, crs=4326).set_geometry('geometry')

    print("GeoDataFrame with tile positions inside numpy contour bool_array created.")

    return np_bool_grid, gdf_numpy_positions


def check_poly_pos(rows_range, columns_range, y, x, tile_height, tile_width):
    poly_i_r = 0  # row
    poly_i_c = 0  # column

    # rows_range -> scan from top to bottom, from max to min
    for i_r, row in enumerate(rows_range):
        if row > y > row - tile_height:
            poly_i_r = i_r
            break

    # columns_range -> scan from left to right, from min to max
    for i_c, column in enumerate(columns_range):
        if column < x < column + tile_width:
            poly_i_c = i_c
            break

    return poly_i_r, poly_i_c


def get_random_start_points_list(number_of_start_points: int, area_bool: np.ndarray):
    start_coordinates = set()  # if a set, then no duplicates, but unordered and unindexed
    rows, cols = area_bool.shape
    available_cells = np.count_nonzero(area_bool)

    while True:
        random_row = np.random.randint(0, rows)
        random_col = np.random.randint(0, cols)
        if area_bool[random_row, random_col]:
            start_coordinates.add((random_row, random_col))
        if len(start_coordinates) >= available_cells or number_of_start_points == len(start_coordinates):
            break

    return list(start_coordinates)  # back to list, because we need an index later


def worker(input_queue, output_queue):
    """
    Necessary worker for python multiprocessing
    Has an Index, if needed...
    """
    for idx, func, args in iter(input_queue.get, 'STOP'):
        result = func(*args)
        output_queue.put([idx, result])


def search_worker(input_queue, output_queue, list_too_search):
    """
    Necessary worker for python multiprocessing
    Has an Index, if needed...
    """
    for idx, func, args in iter(input_queue.get, 'STOP'):
        result = func(list_too_search, *args)
        output_queue.put([idx, result])
